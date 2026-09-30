"""Firmware integration checks use a fully synthetic serial device only."""
import json
import os
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from dmimu import v2
from dmimu.firmware import ENTER_UPGRADE, REBOOT
from dmimu.protocol import crc16
from dmimu.service import Service, Fault
from dmimu.storage import Settings, windows_powershell
from dmimu.web import create_app
from tests.test_service import FakeSerial, port


def reply(command, payload):
    body = bytes((command, 0)) + struct.pack('<H', len(payload)) + payload
    return b'\xa5' + body + struct.pack('<H', crc16(body)) + b'\x5a'


def boot(command, value=0):
    return bytes((0x55, 0xaa, 1, command)) + value.to_bytes(3, 'big') + b'\x0a'


def package(version=(2, 0, 4, 0), body=b'\x37' * 4101):
    return body + b'\x55\xaa' + bytes(version) + bytes(4) + b'20261001'


class BootSerial(FakeSerial):
    ignore_enter = False
    post_version = (2, 0, 4, 0)
    short_page = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.version = (2, 0, 3, 0)
        self.reboots = 0

    def write(self, raw):
        super().write(raw)
        if raw.startswith(b'\xa5'):
            command = raw[1]
            if command == v2.READ_VERSION:
                self.data.extend(reply(command, bytes(4) + bytes(self.version)))
            elif command == v2.BUILD_INFO:
                self.data.extend(reply(command,b'\0 20260922  \0'))
            elif command == v2.READ_CONFIGURATION:
                config = bytes((1,1,1,1,0,0)) + struct.pack('<H', 10) + bytes((0,45)) + struct.pack('<HH',1,0) + bytes((0,6,0,0,0,0,0,0))
                self.data.extend(reply(command, config))
        elif raw == ENTER_UPGRADE:
            if not self.ignore_enter:
                self.data.extend(boot(0xaa))
        elif raw[:2] in (b'\xaa\xdd', b'\xaa\xee'):
            self.data.extend(boot(0xaa))
        elif raw[:2] == b'\xaa\xff':
            if self.short_page:
                return len(raw)-1
            # A two-page synthetic image, with last-page BB/CC in one RX batch.
            self.data.extend(bytes((0x55,0xaa,1,0xbb,raw[2],0,0,0x0a)))
            if raw[2] == 2:
                self.data.extend(boot(0xcc, 3))
        elif raw == REBOOT:
            self.reboots += 1
            self.version = self.post_version
        return len(raw)


class FirmwareRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(self.temp.name)
        self.service = Service(self.settings, serial_factory=BootSerial, port_provider=lambda:[port()])
        self.service.auto_connect = False
        self.service._connect('/fake/imu', self.service.ports()[0]['identity'])
        self.service.device = {'version': {'app':[2,0,3,0], 'app_text':'2.0.3.0'}, 'protocol':'v2'}
        self.client = create_app(self.settings,self.service).test_client()
        self.headers = {'Authorization': 'Bearer '+self.settings.values['agent_token']}
        self.identifier = self.service.store_firmware(package(), '../official.bin')['id']

    def tearDown(self):
        self.service.close();self.temp.cleanup()

    def params(self):
        return dict(id=self.identifier, acknowledged=True, expected_version='2.0.3.0', expected_identity=self.service.identity)

    def test_cancel_acknowledged_prevents_new_boot_writes(self):
        link = self.service.serial
        self.service.firmware_reserved = 'synthetic-upgrade'
        result = self.service.submit('firmware.cancel', {'operation_id': 'synthetic-upgrade'}, 'cancel-no-new-packet')
        self.assertEqual(result['state'], 'succeeded')
        self.assertFalse(self.service._write_firmware(ENTER_UPGRADE))
        self.assertFalse(self.service._write_firmware(REBOOT))
        self.assertEqual(link.writes, [])

    def wait_operation(self, identifier, timeout=5):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            op=self.service.operation(identifier)
            if op['state'] not in ('queued','running'):return op
            time.sleep(.01)
        self.fail('synthetic operation did not finish')

    def test_full_ack_driven_upgrade_and_post_reboot_readback(self):
        self.service.start()
        response=self.client.post('/api/agent/v1/actions',json={'action':'firmware.upgrade','params':self.params()},headers=self.headers|{'Idempotency-Key':'upgrade-1'})
        op=self.wait_operation(response.json['operation']['id'])
        self.assertEqual(op['state'],'succeeded',op)
        self.assertTrue(op['result']['firmware']['version_verified'])
        self.assertFalse(op['result']['firmware']['hardware_flash_verified'])
        serial=self.service.serial
        boot_writes=[raw for raw in serial.writes if raw[0]==0xaa]
        self.assertEqual([raw[:2] for raw in boot_writes],[b'\xaa\x06',b'\xaa\xdd',b'\xaa\xee',b'\xaa\xff',b'\xaa\xff',b'\xaa\x00'])
        self.assertEqual(serial.reboots,1)
        self.assertEqual(serial.writes[-1],v2.request(v2.READ_VERSION))
        self.assertEqual(self.service.device['version']['app_text'],'2.0.4.0')
        again=self.client.post('/api/agent/v1/actions',json={'action':'firmware.upgrade','params':self.params()},headers=self.headers|{'Idempotency-Key':'upgrade-1'})
        self.assertEqual(again.json['operation']['id'],op['id'])
        self.assertEqual(serial.reboots,1)

    def test_equal_version_fails_before_any_bootloader_write(self):
        identifier=self.service.store_firmware(package((2,0,3,0)))['id']
        self.service.start()
        op=self.service.submit('firmware.upgrade',self.params()|{'id':identifier},'same-version')
        final=self.wait_operation(op['id'])
        self.assertEqual(final['error']['code'],'VERSION_NOT_NEWER')
        self.assertFalse(any(raw[0]==0xaa for raw in self.service.serial.writes))

    def test_target_identity_and_explicit_acknowledgment_guard(self):
        self.service.start()
        for change,expected in [({'acknowledged':False},'ACKNOWLEDGEMENT_REQUIRED'),({'expected_identity':'other'},'DEVICE_CHANGED'),({'expected_version':'2.0.2.0'},'VERSION_CHANGED')]:
            op=self.service.submit('firmware.upgrade',self.params()|change,expected)
            self.assertEqual(self.wait_operation(op['id'])['error']['code'],expected)
        self.assertFalse(any(raw[0]==0xaa for raw in self.service.serial.writes))

    def test_cancel_is_immediate_and_prevents_remaining_packets(self):
        self.service.serial.ignore_enter=True
        self.service.start()
        op=self.service.submit('firmware.upgrade',self.params(),'cancel-running')
        deadline=time.monotonic()+3
        while ENTER_UPGRADE not in self.service.serial.writes and time.monotonic()<deadline:time.sleep(.005)
        with self.assertRaises(Fault) as caught:self.service.submit('device.yaw-zero',{'acknowledged':True},'conflict')
        self.assertEqual(caught.exception.code,'FIRMWARE_BUSY')
        cancel=self.service.submit('firmware.cancel',{'operation_id':op['id']},'cancel')
        self.assertEqual(cancel['state'],'succeeded')
        final=self.wait_operation(op['id'])
        self.assertEqual(final['state'],'uncertain')
        self.assertTrue(final['result']['cancelled'])
        self.assertEqual([raw for raw in self.service.serial.writes if raw[0]==0xaa],[ENTER_UPGRADE])
        self.assertEqual(self.service.serial.reboots,0)

    def test_cancel_queued_upgrade_never_writes(self):
        op=self.service.submit('firmware.upgrade',self.params(),'queued-upgrade')
        self.service.submit('firmware.cancel',{'operation_id':op['id']},'queued-cancel')
        self.service.start()
        final=self.wait_operation(op['id'])
        self.assertTrue(final['result']['cancelled'])
        self.assertFalse(final['result']['uncertain'])
        self.assertEqual(self.service.serial.writes,[])

    def test_recording_conflict_precedes_queries(self):
        self.service.recordings.start('live','fake',False)
        self.service.start()
        op=self.service.submit('firmware.upgrade',self.params(),'recording')
        final=self.wait_operation(op['id'])
        self.assertEqual(final['error']['code'],'DEVICE_BUSY')
        self.assertEqual(self.service.serial.writes,[])

    def test_version_mismatch_after_one_reboot_is_uncertain(self):
        self.service.serial.post_version=(2,0,3,0)
        self.service.start()
        final=self.wait_operation(self.service.submit('firmware.upgrade',self.params(),'mismatch')['id'])
        self.assertEqual(final['state'],'uncertain')
        self.assertEqual(final['result']['firmware']['error']['code'],'VERSION_READBACK_MISMATCH')
        self.assertEqual(self.service.serial.reboots,1)

    def test_short_page_write_is_uncertain_and_never_reboots(self):
        self.service.serial.short_page=True
        self.service.start()
        final=self.wait_operation(self.service.submit('firmware.upgrade',self.params(),'short-write')['id'])
        self.assertEqual(final['state'],'uncertain')
        self.assertEqual(final['result']['firmware']['error']['code'],'SERIAL_WRITE_FAILED')
        self.assertEqual(self.service.serial.reboots,0)

    def test_usb_reenumeration_reconnects_only_the_confirmed_identity(self):
        created=[]
        class Reenumerating(BootSerial):
            def read(inner,n):
                if inner.reboots:
                    raise OSError('synthetic USB reboot disconnect')
                return super().read(n)
        def factory(**kwargs):
            link=Reenumerating(**kwargs) if not created else BootSerial(**kwargs)
            if created:link.version=(2,0,4,0)
            created.append(link);return link
        self.service._close_serial()
        self.service.serial_factory=factory
        self.service._connect('/fake/imu',self.service.ports()[0]['identity'])
        self.service.start()
        final=self.wait_operation(self.service.submit('firmware.upgrade',self.params(),'reenumerate')['id'])
        self.assertEqual(final['state'],'succeeded',final)
        self.assertEqual(len(created),2)
        self.assertEqual(created[0].reboots,1)
        self.assertEqual(created[1].writes,[v2.request(v2.READ_VERSION)])
        self.assertEqual(self.service.identity,self.params()['expected_identity'])

    def test_boot_timeout_retries_bounded_packet_and_never_replays_upgrade(self):
        from dmimu.firmware import UpgradeSession
        self.service.serial.ignore_enter=True
        self.service.start()
        def fast(*args,**kwargs):return UpgradeSession(*args,**kwargs,timeout=.03,erase_timeout=.1,max_attempts=2)
        with patch('dmimu.firmware_runtime.UpgradeSession',side_effect=fast):
            final=self.wait_operation(self.service.submit('firmware.upgrade',self.params(),'timeout')['id'])
        self.assertEqual(final['state'],'uncertain')
        self.assertEqual(final['result']['firmware']['error']['code'],'ACK_TIMEOUT')
        self.assertEqual([raw for raw in self.service.serial.writes if raw[0]==0xaa],[ENTER_UPGRADE]*2)
        self.assertEqual(self.service.serial.reboots,0)

    def test_upload_private_integrity_and_auth(self):
        self.assertEqual(self.client.post('/api/agent/v1/firmware/upload',data=package(),content_type='application/octet-stream').status_code,401)
        uploaded=self.client.post('/api/agent/v1/firmware/upload',data=package(),content_type='application/octet-stream',headers=self.headers|{'X-Firmware-Name':'../../test.bin'})
        data=uploaded.json['data'];self.assertEqual(data['filename'],'test.bin');self.assertTrue(data['upgrade_allowed'])
        self.assertEqual(data['current_version'],'2.0.3.0')
        if os.name == 'nt':
            quoted = str(self.service._firmware_path(data['id'])).replace("'", "''")
            script = f"$p='{quoted}'; $sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User; $a=Get-Acl -LiteralPath $p; "
            script += "if($a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $sid.Value){throw 'Wrong owner'}; foreach($r in $a.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier])){if($r.AccessControlType -eq 'Allow' -and $r.IdentityReference.Value -ne $sid.Value){throw 'Shared access'}}"
            windows_powershell(script)
        else:
            self.assertEqual(self.service._firmware_path(data['id']).stat().st_mode&0o077,0)
        listing=self.client.get('/api/agent/v1/firmware',headers=self.headers).json['data']
        self.assertEqual(len(listing),2)
        self.service._firmware_path(data['id']).write_bytes(package(body=b'changed'))
        with self.assertRaises(Fault) as exc:self.service.inspect_firmware(data['id'])
        self.assertEqual(exc.exception.code,'FIRMWARE_CHANGED')
        self.assertEqual(self.service.serial.writes,[])

    def test_binary_upload_limit_and_invalid_footer(self):
        response=self.client.post('/api/agent/v1/firmware/upload',data=b'bad',content_type='application/octet-stream',headers=self.headers)
        self.assertEqual(response.status_code,400)
        response=self.client.post('/api/agent/v1/firmware/upload',data=b'x'*(4096*255+19),content_type='application/octet-stream',headers=self.headers)
        self.assertEqual(response.status_code,413)

    def test_config_exit_failure_is_uncertain_and_generation_invalidates(self):
        config=bytes((1,1,1,1,0,0))+struct.pack('<H',10)+bytes((0,45))+struct.pack('<HH',1,0)+bytes((0,6,0,1,0,0,0,0))
        self.service.device['configuration']={'installation_rotation':0,'accel_range':0,'gyro_range':0}
        def query(command,payload=b'',**kwargs):
            if command==2 and payload==b'\0':raise Fault('DEVICE_ACK_TIMEOUT','exit failed')
            return config if command==v2.READ_CONFIGURATION else b''
        before=self.service.generation
        with patch.object(self.service,'_v2_request',side_effect=query):
            result=self.service._perform('device.configure',{'installation_rotation':1})
        self.assertTrue(result['uncertain']);self.assertFalse(result['exit_acknowledged'])
        self.assertGreater(self.service.generation,before)

    def test_build_info_readonly_text_and_raw_bytes(self):
        self.service.start()
        final=self.wait_operation(self.service.submit('device.build-info',{},'build-info')['id'])
        self.assertEqual(final['state'],'succeeded')
        self.assertEqual(final['result']['build_info']['text'],'20260922')
        self.assertEqual(self.service.serial.writes,[v2.request(v2.BUILD_INFO)])
        self.assertIsNone(v2.build_info(b'\xff')['text'])
        self.assertEqual(v2.build_info(b'\xff')['raw_hex'],'ff')

    def test_restart_sends_once_then_reads_the_same_device_version(self):
        self.service.serial.post_version=(2,0,3,0)
        self.service.start()
        final=self.wait_operation(self.service.submit('device.restart',{'acknowledged':True},'restart')['id'])
        self.assertEqual(final['state'],'succeeded',final)
        self.assertEqual(self.service.serial.writes,[v2.request(v2.READ_VERSION),REBOOT,v2.request(v2.READ_VERSION)])
        self.assertFalse(final['result']['physical_restart_verified'])
        repeated=self.service.submit('device.restart',{'acknowledged':True},'restart')
        self.assertEqual(repeated['id'],final['id'])
        self.assertEqual(self.service.serial.reboots,1)

    def test_restart_version_mismatch_preserves_uncertain_without_retry(self):
        self.service.start()
        final=self.wait_operation(self.service.submit('device.restart',{'acknowledged':True},'restart-mismatch')['id'])
        self.assertEqual(final['state'],'uncertain')
        self.assertEqual(self.service.serial.reboots,1)
        self.assertEqual(self.service.connection,'restart-uncertain')

    def test_restart_requires_boolean_confirmation_before_any_write(self):
        for value in (False,1,'true'):
            with self.assertRaises(Fault):self.service._perform('device.restart',{'acknowledged':value})
        self.assertEqual(self.service.serial.writes,[])

    def test_private_recording_import_and_native_download_routes(self):
        from dmimu.protocol import encode_frame
        metadata=json.dumps(dict(StartedUtc='2026-01-01T00:00:00Z', Source='synthetic',Protocol='bosch-api-v1',TimeBasis='host-receive-seconds')).encode()
        raw=encode_frame(1,[1,2,3])
        content=b'IMULOG01'+struct.pack('<i',len(metadata))+metadata+struct.pack('<di',.1,len(raw))+raw+struct.pack('<diQ',.1,-1,1)
        imported=self.client.post('/api/agent/v1/recordings/import',data=content,content_type='application/octet-stream',headers=self.headers)
        self.assertEqual(imported.status_code,200,imported.json)
        self.assertEqual(imported.json['data']['samples'],1)
        downloaded=self.client.get('/api/agent/v1/recordings/'+imported.json['data']['id']+'/imulog',headers=self.headers)
        self.assertTrue(downloaded.data.startswith(b'IMULOG01'))
        self.assertIn(raw,downloaded.data)
        self.assertEqual(self.client.get('/api/agent/v1/recordings/'+'f'*32+'/imulog',headers=self.headers).status_code,404)
        self.assertEqual(self.service.serial.writes,[])

    def test_spectrum_export_route_is_authenticated(self):
        data=dict(format='csv',channel='angular_velocity',source='live',sample_rate_hz=100,window='hann',samples=16,start_time_unix_s=10,end_time_unix_s=10.16,frequency_hz=[i*100/16 for i in range(9)],amplitude=[[0]*9 for _ in range(3)])
        self.assertEqual(self.client.post('/api/agent/v1/spectra/export',json=data).status_code,401)
        exported=self.client.post('/api/agent/v1/spectra/export',json=data,headers=self.headers)
        self.assertEqual(exported.status_code,200,exported.json)
        self.assertIn(b'frequency_hz',exported.data)

    def test_queued_allan_cancel_is_out_of_band(self):
        op=self.service.submit('allan.analyze',{'recording_id':'a'*32,'channel':'gyro','sample_rate':100},'queued-analysis')
        cancel=self.service.submit('allan.cancel',{'operation_id':op['id']},'cancel-analysis')
        self.assertEqual(cancel['state'],'succeeded')
        self.service.start()
        final=self.wait_operation(op['id'])
        self.assertEqual(final['error']['code'],'CANCELLED')


if __name__=='__main__':unittest.main()
