const filters = document.getElementById('analytics-filters');
if (filters) {
  filters.addEventListener('change', () => filters.requestSubmit());
  filters.querySelector('button').hidden = true;
}
