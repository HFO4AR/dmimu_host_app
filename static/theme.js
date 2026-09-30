try {
  document.documentElement.dataset.theme = localStorage.getItem('dmimu.theme') || 'light';
  document.documentElement.dataset.accent = localStorage.getItem('dmimu.accent') || 'coral';
} catch (_) { /* Browser storage can be disabled. */ }
