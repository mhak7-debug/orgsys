(function () {
  var c = document.getElementById('pad');
  if (!c) return;
  var ctx = c.getContext('2d'), drawing = false, empty = true;
  ctx.lineWidth = 3; ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.strokeStyle = '#10286b';
  function pos(e) {
    var r = c.getBoundingClientRect();
    return { x: (e.clientX - r.left) * c.width / r.width, y: (e.clientY - r.top) * c.height / r.height };
  }
  c.addEventListener('pointerdown', function (e) {
    drawing = true; c.setPointerCapture(e.pointerId);
    var p = pos(e); ctx.beginPath(); ctx.moveTo(p.x, p.y); e.preventDefault();
  });
  c.addEventListener('pointermove', function (e) {
    if (!drawing) return;
    var p = pos(e); ctx.lineTo(p.x, p.y); ctx.stroke(); empty = false; e.preventDefault();
  });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (n) {
    c.addEventListener(n, function () { drawing = false; });
  });
  document.getElementById('clear').addEventListener('click', function () {
    ctx.clearRect(0, 0, c.width, c.height); empty = true;
  });
  document.getElementById('sigform').addEventListener('submit', function (e) {
    if (empty) { e.preventDefault(); alert('ارسم توقيعك أولاً'); return; }
    document.getElementById('data').value = c.toDataURL('image/png');
  });
})();
