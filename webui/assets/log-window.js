// Follow newly appended output only while the operational window is expanded.
document.querySelectorAll('.operational-window').forEach(panel => {
  const output = panel.querySelector('pre');
  const follow = panel.querySelector('input[type="checkbox"]');
  let scrollPending = false;
  function scrollToLatest() {
    if (scrollPending || !panel.open || !follow.checked || document.hidden) return;
    scrollPending = true;
    window.requestAnimationFrame(() => {
      scrollPending = false;
      // The user may have closed the log or disabled following since scheduling.
      if (panel.open && follow.checked && !document.hidden) output.scrollTop = output.scrollHeight;
    });
  }
  new MutationObserver(scrollToLatest).observe(output, { childList: true, characterData: true, subtree: true });
  panel.addEventListener('toggle', scrollToLatest);
  follow.addEventListener('change', scrollToLatest);
  scrollToLatest();
});
