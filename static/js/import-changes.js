(() => {
  'use strict';
  const root = document.querySelector('[data-import-changes]');
  const source = document.getElementById('importChangesData');
  if (!root || !source) return;
  const changes = JSON.parse(source.textContent);
  const body = root.querySelector('[data-changes-body]');
  const search = root.querySelector('[data-change-search]');
  const filter = root.querySelector('[data-change-filter]');
  const previous = root.querySelector('[data-changes-prev]');
  const next = root.querySelector('[data-changes-next]');
  let page = 0;
  const description = row => row ? `${row.day_label} · الحصة ${row.period_index}\n${row.subject_name}\n${row.teacher_name}${row.is_active ? '' : '\nغير مفعّلة'}` : '—';
  const render = () => {
    const term = search.value.trim().toLocaleLowerCase();
    const shown = changes.filter(change => (filter.value === 'all' || change.kind === filter.value) &&
      (!term || [change.label, ...[change.before, change.after].filter(Boolean).flatMap(row =>
        ['class_name', 'day_label', 'period_index', 'subject_name', 'teacher_name'].map(field => row[field]))].join(' ').toLocaleLowerCase().includes(term)));
    page = Math.max(0, Math.min(page, Math.ceil(shown.length / 100) - 1));
    body.replaceChildren();
    shown.slice(page * 100, (page + 1) * 100).forEach(change => {
      const tr = document.createElement('tr');
      tr.dataset.kind = change.kind;
      [change.label, (change.after || change.before).class_name, description(change.before), description(change.after)].forEach(value => {
        const td = document.createElement('td');
        td.textContent = value;
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    previous.disabled = page === 0;
    next.disabled = (page + 1) * 100 >= shown.length;
    root.querySelector('[data-changes-page]').textContent = shown.length ? `${page * 100 + 1}–${Math.min((page + 1) * 100, shown.length)} من ${shown.length}` : 'لا توجد تغييرات مطابقة';
  };
  search.addEventListener('input', () => { page = 0; render(); });
  filter.addEventListener('change', () => { page = 0; render(); });
  previous.addEventListener('click', () => { page--; render(); });
  next.addEventListener('click', () => { page++; render(); });
  render();
})();
