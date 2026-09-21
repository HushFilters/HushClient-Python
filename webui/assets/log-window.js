// Follow newly appended output only while the operational window is expanded.
document.querySelectorAll('.operational-window').forEach(panel => {
  const output = panel.querySelector('pre');
  const follow = panel.querySelector('input[type="checkbox"]');
  let previousText = output.textContent;
  function scrollToLatest() {
    if (panel.open && follow.checked) {
      window.requestAnimationFrame(() => { output.scrollTop = output.scrollHeight; });
    }
  }
  new MutationObserver(() => {
    if (output.textContent !== previousText) {
      previousText = output.textContent;
      scrollToLatest();
    }
  }).observe(output, { childList: true, characterData: true, subtree: true });
  panel.addEventListener('toggle', scrollToLatest);
  follow.addEventListener('change', scrollToLatest);
  scrollToLatest();
});
