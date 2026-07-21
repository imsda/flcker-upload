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

document.querySelectorAll('[data-folder-toolbar]').forEach((toolbar) => {
  const list = document.querySelector('[data-folder-list]');
  const search = toolbar.querySelector('[data-folder-search]');
  const filter = toolbar.querySelector('[data-folder-filter]');
  const clear = toolbar.querySelector('[data-folder-clear]');
  const count = document.querySelector('[data-folder-count]');
  const rows = Array.from(list?.querySelectorAll('[data-folder-row]') || []);
  const noResults = list?.querySelector('[data-folder-no-results]');

  const updateFolders = () => {
    const query = search.value.trim().toLocaleLowerCase();
    const selectedFilter = filter.value;
    let visible = 0;

    rows.forEach((row) => {
      const matchesSearch = row.dataset.folderName.includes(query);
      const matchesFilter = selectedFilter === 'all'
        || row.dataset.folderLocation === selectedFilter
        || row.dataset.folderAccess === selectedFilter;
      const matches = matchesSearch && matchesFilter;
      row.hidden = !matches;
      if (matches) visible += 1;
    });

    if (count) count.textContent = `${visible} ${visible === 1 ? 'folder' : 'folders'}`;
    if (noResults) noResults.hidden = visible !== 0;
    clear.hidden = query === '' && selectedFilter === 'all';
  };

  search.addEventListener('input', updateFolders);
  filter.addEventListener('change', updateFolders);
  clear.addEventListener('click', () => {
    search.value = '';
    filter.value = 'all';
    updateFolders();
    search.focus();
  });
});
