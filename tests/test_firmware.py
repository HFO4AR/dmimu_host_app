"""Offline boot package/ACK/retry tests; no serial transport is opened."""
import binascii
import struct
import unittest

from dmimu.firmware import (BootAckDecoder, ENTER_UPGRADE, REBOOT, FirmwareError, FirmwareImage,
                            MAX_PACKAGE_SIZE, PAGE_SIZE, UpgradeSession, crc16_native_legacy, version_tuple)


def package(body=b'firmware', version=(2,0,4,0), boot=(0,0,0,0), release=b'20261001'):
    return body + b'\x55\xaa' + bytes(version) + bytes(boot) + release


def ack(command=0xaa, value=0, bitmap=None, device_id=1):
    values = bitmap.to_bytes(3,'big') if bitmap is not None else bytes([value,0,0])
    return b'\x55\xaa' + bytes([device_id,command]) + values + b'\x0a'


class PackageTests(unittest.TestCase):
    def test_footer_parses_and_body_is_opaque(self):
        raw = package(bytes(range(256)) * 17)
        image = FirmwareImage.from_bytes(raw)
        self.assertEqual(image.app_version, (2,0,4,0))
        self.assertEqual(image.boot_version, (0,0,0,0))
        self.assertEqual(image.release,'20261001')
        self.assertEqual(image.body,raw[:-18])
        self.assertEqual(image.page_count,2)
        self.assertEqual(image.page(2)[256:],b'\xff'*(4096-256))

    def test_page_crc_is_only_over_4096_payload(self):
        image = FirmwareImage.from_bytes(package(bytes(range(256))*16))
        wire = image.page_packet(1)
        self.assertEqual(len(wire),4104)
        self.assertEqual(wire[:5],b'\xaa\xff\x01\x00\x10')
        self.assertEqual(wire[5:4101],image.body)
        self.assertEqual(struct.unpack('<H',wire[-3:-1])[0],binascii.crc_hqx(image.body,0xffff))
        self.assertEqual(wire[-1],13)

    def test_metadata_and_erase_layout(self):
        raw = package(b'\x31' * 70000)
        image = FirmwareImage.from_bytes(raw)
        self.assertEqual(image.metadata_packet(),b'\xaa\xdd'+raw[-16:]+b'\x0d')
        self.assertEqual(len(image.metadata_packet()),19)
        self.assertEqual(image.erase_packet(),b'\xaa\xee'+bytes([18])+struct.pack('<H',len(raw)&0xffff)+b'\x0d')

    def test_malformed_footer_and_bounds(self):
        for raw in (b'',b'\x55\xaa'+b'\x00'*16,package()[:-1],b'x'*(MAX_PACKAGE_SIZE+1)):
            with self.subTest(size=len(raw)):
                with self.assertRaises(FirmwareError):FirmwareImage.from_bytes(raw)
        with self.assertRaises(FirmwareError):FirmwareImage.from_bytes(package(release=b'\xff'*8))

    def test_equal_older_major_downgrade_and_unknown_crc_blocked(self):
        image = FirmwareImage.from_bytes(package())
        for current in ('2.0.4.0','2.0.5.0','3.0.0.0'):
            with self.subTest(current=current):
                with self.assertRaises(FirmwareError):image.upgrade_guard(current)
        self.assertFalse(image.inspect('2.0.4.0')['upgrade_allowed'])
        self.assertTrue(image.inspect('2.0.3.0')['upgrade_allowed'])
        with self.assertRaises(FirmwareError):UpgradeSession(image,'1.0.0.0',1)
        old = FirmwareImage.from_bytes(package(version=(1,0,9,0)))
        with self.assertRaises(FirmwareError) as cm:old.upgrade_guard('2.0.3.0')
        self.assertEqual(cm.exception.code,'V2_DOWNGRADE_FORBIDDEN')
        with self.assertRaises(FirmwareError):image.page_packet(1,'legacy')

    def test_native_legacy_crc_is_not_standard_ccitt(self):
        # Constants independently checked with C translation of native table/fold.
        self.assertEqual(crc16_native_legacy(b'123456789'),0x5f16)
        self.assertEqual(crc16_native_legacy(bytes(range(256))*16),0x3ad4)
        image=FirmwareImage.from_bytes(package(bytes(range(256))*16,version=(1,0,5,0)))
        wire=image.page_packet(1,'native-legacy')
        self.assertEqual(wire[-3:-1],bytes.fromhex('D4 3A'))
        session=UpgradeSession(image,'1.0.4.0',1,crc_mode='native-legacy')
        self.assertEqual(session.crc_mode,'native-legacy')

    def test_versions_and_page_numbers_are_strict(self):
        self.assertEqual(version_tuple('V2.0.3.0'),(2,0,3,0))
        for v in ('2.0.3','2.x.3.0',(-1,0,0,0),(True,0,0,0),[2,0,0,256]):
            with self.assertRaises(FirmwareError):version_tuple(v)
        image = FirmwareImage.from_bytes(package())
        for index in (0,-1,2,True,1.5):
            with self.assertRaises(FirmwareError):image.page(index)


class AckTests(unittest.TestCase):
    def test_split_resync_and_byte_order(self):
        decoder = BootAckDecoder()
        wire = b'junk\x55' + ack(0xbb,7) + ack(0xcc,bitmap=0x810203)
        frames=[]
        for byte in wire:frames.extend(decoder.feed(bytes([byte])))
        self.assertEqual([(x.command,x.value) for x in frames],[(0xbb,7),(0xcc,0x81)])
        self.assertEqual(frames[1].bitmap,0x810203)
        self.assertGreater(decoder.discarded,0)
        self.assertEqual(len(decoder.buffer),0)

    def test_bad_end_and_measurement_type_not_boot_ack(self):
        decoder=BootAckDecoder()
        self.assertEqual(decoder.feed(ack()[:-1]+b'\xff'),[])
        self.assertEqual(decoder.feed(b'\x55\xaa\x01\x01'+b'\x00'*14+b'\x0a'),[])
        self.assertEqual(len(decoder.feed(ack())),1)


class SessionTests(unittest.TestCase):
    def session(self, pages=2):
        image = FirmwareImage.from_bytes(package(b'\x31'*(pages*PAGE_SIZE)))
        return UpgradeSession(image,'2.0.3.0',1)

    def writing(self, session):
        self.assertEqual(session.start(0).packet,ENTER_UPGRADE)
        self.assertEqual(session.feed(ack(),.1).stage,'announcing')
        self.assertEqual(session.feed(ack(),.2).stage,'erasing')
        self.assertEqual(session.feed(ack(),.3).stage,'writing')

    def test_ack_driven_flow_requires_version_readback(self):
        s=self.session();self.writing(s)
        self.assertIsNone(s.feed(ack(0xbb,9),.4))
        self.assertEqual(s.acked_pages,0)
        self.assertEqual(s.feed(ack(0xbb,1),.5).page,2)
        self.assertEqual(s.feed(ack(0xbb,2)+ack(0xcc,bitmap=3),.6).packet,REBOOT)
        self.assertEqual(s.state,'waiting_reconnect')
        self.assertFalse(s.version_verified)
        self.assertTrue(s.snapshot()['write_result_uncertain'])
        self.assertTrue(s.verify_reconnected_version('2.0.4.0'))
        self.assertEqual(s.state,'succeeded')
        self.assertFalse(s.snapshot()['write_result_uncertain'])
        self.assertFalse(s.snapshot()['hardware_flash_verified'])

    def test_old_generic_ack_batch_cannot_skip_stages(self):
        s=self.session();s.start(0)
        outgoing=s.feed(ack()*3,.1)
        self.assertEqual(outgoing.stage,'announcing')
        self.assertEqual(s.state,'announcing')
        self.assertEqual(s.feed(b'',.2),None)

    def test_timeout_bounded_retry_and_terminal_failure(self):
        s=self.session();s.start(0)
        for i in range(1,5):
            out=s.tick(i*2)
            self.assertEqual(out.packet,ENTER_UPGRADE)
            self.assertEqual(out.attempt,i+1)
        self.assertIsNone(s.tick(10))
        self.assertEqual(s.state,'failed')
        self.assertEqual(s.retries,4)
        self.assertEqual(s.error['code'],'ACK_TIMEOUT')
        self.assertIsNone(s.feed(ack(),11))

    def test_erase_timeout_and_duplicate_page_ack(self):
        s=self.session();self.writing(s)
        s.feed(ack(0xbb,1),.4)
        self.assertIsNone(s.feed(ack(0xbb,1),.5))
        self.assertEqual(s.acked_pages,1)
        s.feed(ack(0xbb,2),.6)
        self.assertIsNone(s.tick(15.6))
        self.assertEqual(s.error['code'],'COMPLETION_TIMEOUT')

    def test_wrong_device_ack_ignored(self):
        s=self.session();s.start(0)
        self.assertIsNone(s.feed(ack(device_id=2),.1))
        self.assertEqual(s.state,'entering')

    def test_bitmap_count_must_cover_all_pages(self):
        s=self.session();self.writing(s)
        s.feed(ack(0xbb,1),.4);s.feed(ack(0xbb,2),.5)
        s.feed(ack(0xcc,bitmap=1),.6)  # one complete bit for a two-page file
        self.assertEqual(s.error['code'],'INCOMPLETE_BITMAP')

    def test_bitmap_bit_order_is_not_invented(self):
        s=self.session();self.writing(s)
        s.feed(ack(0xbb,1),.4);s.feed(ack(0xbb,2),.5)
        self.assertEqual(s.feed(ack(0xcc,bitmap=5),.6).packet,REBOOT)
        self.assertEqual(s.state,'waiting_reconnect')
        self.assertFalse(s.version_verified)

    def test_more_than_24_pages_cannot_claim_bitmap_coverage(self):
        s=self.session(pages=26);self.writing(s)
        for i in range(1,27):s.feed(ack(0xbb,i),.3+i*.01)
        s.feed(ack(0xcc,bitmap=0xffffff),.9)
        self.assertEqual(s.state,'waiting_reconnect')
        self.assertFalse(s.snapshot()['bitmap_covers_all_pages'])
        self.assertFalse(s.version_verified)

    def test_reconnect_mismatch_and_cancel_are_uncertain(self):
        s=self.session(pages=1);self.writing(s)
        s.feed(ack(0xbb,1)+ack(0xcc,bitmap=1),.4)
        self.assertFalse(s.verify_reconnected_version('2.0.3.0'))
        self.assertEqual(s.error['code'],'VERSION_READBACK_MISMATCH')
        s=self.session();s.start(0);s.cancel()
        self.assertEqual(s.state,'cancelled')
        self.assertIsNone(s.tick(100))
        self.assertTrue(s.snapshot()['write_result_uncertain'])

    def test_fake_serial_exercises_whole_transaction(self):
        session=self.session()
        class FakeSerial:
            def __init__(self):self.writes=[];self.page_count=0
            def exchange(self,packet):
                self.writes.append(packet)
                if packet==ENTER_UPGRADE:return ack()
                if packet==REBOOT:return b''
                if packet[:2]==b'\xaa\xdd':
                    self.assert_size(packet,19);return ack()
                if packet[:2]==b'\xaa\xee':
                    self.assert_size(packet,6);self.page_count=packet[2];return ack()
                if packet[:2]==b'\xaa\xff':
                    self.assert_size(packet,4104)
                    if int.from_bytes(packet[-3:-1],'little')!=binascii.crc_hqx(packet[5:-3],0xffff):return b''
                    result=ack(0xbb,packet[2])
                    if packet[2]==self.page_count:result+=ack(0xcc,bitmap=(1<<self.page_count)-1)
                    return result
                raise AssertionError('Unexpected packet')
            def assert_size(self,packet,expected):
                if len(packet)!=expected:raise AssertionError('Wrong packet length')
        transport=FakeSerial();out=session.start(0);now=.01
        while out:
            response=transport.exchange(out.packet);out=session.feed(response,now);now+=.01
        self.assertEqual([x[:2].hex() for x in transport.writes],['aa06','aadd','aaee','aaff','aaff','aa00'])
        self.assertEqual(session.state,'waiting_reconnect')
        self.assertTrue(session.verify_reconnected_version('2.0.4.0'))

    def test_exact_deadline_causes_retry_instead_of_advancing(self):
        s=self.session();s.start(0)
        out=s.feed(ack(),2)
        self.assertEqual(out.attempt,2)
        self.assertEqual(s.state,'entering')

    def test_readback_not_accepted_before_completion(self):
        s=self.session()
        with self.assertRaises(FirmwareError):s.verify_reconnected_version('2.0.4.0')
        with self.assertRaises(FirmwareError):UpgradeSession(s.image,'2.0.3.0',1,max_attempts=6)


if __name__ == '__main__':unittest.main()
