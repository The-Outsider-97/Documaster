// The start-up page. The shell replaces it with Documaster as soon as the engine is listening. If the engine cannot start, the shell
// calls window.documasterFailed(message, detail): the first paragraph of the text is the message, anything after a blank line (the end
// of the engine's log) goes into the box below it.
window.documasterFailed = function (message, detail) {
  var text = String(message || 'Documaster could not start.');
  var split = text.search(/\n\s*\n/);
  var head = split < 0 ? text : text.slice(0, split);
  var rest = (split < 0 ? '' : text.slice(split)).trim();
  var extra = [rest, String(detail || '').trim()].filter(Boolean).join('\n\n');
  document.body.classList.add('failed');
  document.getElementById('msg').textContent = head;
  var box = document.getElementById('detail');
  if (extra) { box.textContent = extra; box.hidden = false; }
};
setTimeout(function () {
  var msg = document.getElementById('msg');
  if (msg && !document.body.classList.contains('failed')) {
    msg.textContent = 'Still starting… the first start after installing can take a minute while Windows checks the new files.';
  }
}, 8000);
