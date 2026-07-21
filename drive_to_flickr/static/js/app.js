document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-copy]');
  if (!button) return;
  const target = document.querySelector(button.dataset.copy);
  if (!target) return;
  await navigator.clipboard.writeText(target.value || target.textContent || '');
  const original = button.textContent;
  button.textContent = 'Copied';
  setTimeout(() => { button.textContent = original; }, 1400);
});
