(() => {
  'use strict';
  const root = document.querySelector('[data-document-import]');
  if (!root) return;
  const extractForm = root.querySelector('[data-extract-form]');
  let extracting = false;
  extractForm.addEventListener('submit', event => {
    if (extracting) { event.preventDefault(); return; }
    const file = extractForm.querySelector('[name=document]').files[0];
    if (file && file.size > 10 * 1024 * 1024) {
      event.preventDefault();
      const status = root.querySelector('[data-extract-status]');
      status.hidden = false;
      status.textContent = 'حجم الملف يتجاوز 10 م.ب. اختر ملفًا أصغر.';
      return;
    }
    extracting = true;
    extractForm.setAttribute('aria-busy', 'true');
    extractForm.querySelector('button[type=submit]').disabled = true;
    root.querySelector('[data-extract-label]').textContent = 'جارٍ تجهيز الملف…';
    root.querySelector('[data-extract-status]').hidden = false;
  });
  const job = root.querySelector('[data-job]');
  if (job) {
    const progress = job.querySelector('[data-job-progress]');
    const status = job.querySelector('[data-job-status]');
    const error = job.querySelector('[data-job-error]');
    const resume = job.querySelector('[data-job-resume]');
    let advancing = false;
    const advance = async () => {
      if (advancing) return;
      advancing = true;
      resume.hidden = true;
      error.hidden = true;
      const payload = new FormData();
      payload.set('action', 'advance');
      payload.set('job_token', job.dataset.token);
      payload.set('csrfmiddlewaretoken', extractForm.querySelector('[name=csrfmiddlewaretoken]').value);
      try {
        const response = await fetch(location.pathname, { method: 'POST', body: payload, credentials: 'same-origin' });
        if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) throw new Error('تعذر متابعة جلسة القراءة. حدّث الصفحة وسجّل الدخول عند الحاجة.');
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'تعذر استئناف القراءة.');
        if (result.state === 'complete') { location.replace(result.url); return; }
        if (result.total) {
          progress.max = result.total;
          progress.value = result.completed;
          status.textContent = `اكتمل ${result.completed} من ${result.total} جزء. جارٍ قراءة الجزء التالي…`;
        }
        if (result.state === 'paused') throw new Error(result.error || 'توقفت قراءة أحد الأجزاء. أعد المحاولة.');
        advancing = false;
        setTimeout(advance, result.state === 'busy' ? 2500 : 150);
      } catch (failure) {
        error.textContent = failure.message || 'انقطع الاتصال. يمكنك استئناف القراءة دون إعادة الأجزاء المكتملة.';
        error.hidden = false;
        resume.hidden = false;
        advancing = false;
      }
    };
    resume.addEventListener('click', advance);
    if (job.dataset.state !== 'paused') advance();
  }
  const form = root.querySelector('[data-review-form]');
  if (!form) return;
  const rows = JSON.parse(document.getElementById('documentDraftRows').textContent);
  const body = form.querySelector('[data-rows-body]');
  const template = document.getElementById('documentRowTemplate');
  const maxRows = Number(root.dataset.maxRows);
  const status = form.querySelector('[data-editor-status]');
  const mode = form.querySelector('[name=mode]');
  const search = form.querySelector('[data-row-search]');
  const filter = form.querySelector('[data-row-filter]');
  const previous = form.querySelector('[data-page-previous]');
  const next = form.querySelector('[data-page-next]');
  const fields = ['day', 'period_index', 'starts_at', 'ends_at', 'class_name', 'subject_name', 'teacher_name'];
  const metadataFields = ['source', 'note', 'source_id', 'time_source'];
  const bulkField = form.querySelector('[data-bulk-field]');
  const bulkOld = form.querySelector('[data-bulk-old]');
  const bulkNew = form.querySelector('[data-bulk-new]');
  const bulkValues = form.querySelector('#documentBulkValues');
  const pageSize = 100;
  let page = 0;
  let shown = [];
  const dirty = () => {
    const button = root.querySelector('[data-final-import]');
    if (button) { button.disabled = true; root.querySelector('[data-dirty-notice]').hidden = false; }
  };
  const render = () => {
    const term = search.value.trim().toLocaleLowerCase();
    shown = rows.map((data, index) => ({ data, index })).filter(({ data }) => {
      const matches = !term || fields.some(field => String(data[field] || '').toLocaleLowerCase().includes(term));
      const included = filter.value === 'all' || (filter.value === 'errors' ? data.errors?.length : data.errors?.length || data.note);
      return matches && included;
    });
    page = Math.max(0, Math.min(page, Math.ceil(shown.length / pageSize)-1));
    body.replaceChildren();
    shown.slice(page*pageSize, (page+1)*pageSize).forEach(({ data, index }) => {
      const row = template.content.firstElementChild.cloneNode(true);
      row.dataset.rowIndex = String(index);
      fields.forEach(field => {
        const input = row.querySelector(`[data-field="${field}"]`);
        input.value = data[field] || '';
        if (data.time_source === 'school' && (field === 'starts_at' || field === 'ends_at')) input.readOnly = true;
      });
      row.querySelector('[data-source]').textContent = data.source || 'أُضيف يدويًا';
      row.querySelector('[data-note]').textContent = [data.time_source === 'school' ? 'توقيت المدرسة' : '', data.note || ''].filter(Boolean).join(' · ');
      row.querySelector('[data-row-errors]').textContent = (data.errors || []).join(' · ');
      if (data.errors?.length) row.classList.add('di-row-invalid');
      body.appendChild(row);
    });
    root.querySelector('[data-row-count]').textContent = `${rows.length} / ${maxRows}`;
    form.querySelector('[data-add-row]').disabled = rows.length >= maxRows;
    form.querySelector('[data-no-rows]').hidden = shown.length > 0;
    previous.disabled = page === 0;
    next.disabled = (page+1)*pageSize >= shown.length;
    form.querySelector('[data-page-label]').textContent = shown.length ? `${page*pageSize+1}–${Math.min((page+1)*pageSize, shown.length)} من ${shown.length}` : 'لا توجد حصص مطابقة';
  };
  const changeDisplay = () => { page = 0; render(); };
  search.addEventListener('input', changeDisplay);
  filter.addEventListener('change', changeDisplay);
  previous.addEventListener('click', () => { page--; render(); });
  next.addEventListener('click', () => { page++; render(); });
  form.querySelector('[data-add-row]').addEventListener('click', () => {
    if (rows.length >= maxRows) return;
    rows.push({});
    search.value = '';
    filter.value = 'all';
    page = Math.floor((rows.length-1)/pageSize);
    dirty(); render();
    body.lastElementChild?.querySelector('input').focus();
  });
  body.addEventListener('input', event => {
    const input = event.target.closest('[data-field]');
    if (!input) return;
    rows[Number(input.closest('tr').dataset.rowIndex)][input.dataset.field] = input.value.trim();
    dirty();
  });
  body.addEventListener('click', event => {
    const remove = event.target.closest('[data-remove-row]');
    if (!remove) return;
    rows.splice(Number(remove.closest('tr').dataset.rowIndex), 1);
    dirty(); render();
  });
  form.addEventListener('input', event => { if (event.target.matches('textarea[name=teachers],textarea[name=subjects],textarea[name=classes]')) dirty(); });
  mode.addEventListener('change', () => { dirty(); form.querySelector('[data-add-row]').hidden = mode.value === 'entities'; });
  form.querySelector('[data-import-strategy]').addEventListener('change', dirty);
  form.querySelector('[data-add-row]').hidden = mode.value === 'entities';
  const syncBulkValues = () => {
    bulkValues.replaceChildren();
    [...new Set(rows.map(data => data[bulkField.value]).filter(Boolean))].sort().forEach(value => {
      const option = document.createElement('option');
      option.value = value;
      bulkValues.appendChild(option);
    });
  };
  bulkField.addEventListener('change', syncBulkValues);
  form.querySelector('[data-bulk-apply]').addEventListener('click', () => {
    const oldValue = bulkOld.value.trim();
    const newValue = bulkNew.value.trim();
    if (!oldValue || !newValue || oldValue === newValue) { status.textContent = 'حدد الاسم الحالي والاسم الصحيح أولًا.'; return; }
    let changed = 0;
    rows.forEach(data => { if (data[bulkField.value] === oldValue) { data[bulkField.value] = newValue; changed++; } });
    const namesField = { teacher_name: 'teachers', subject_name: 'subjects', class_name: 'classes' }[bulkField.value];
    const extraNames = form.querySelector(`[name="${namesField}"]`);
    extraNames.value = extraNames.value.split('\n').map(value => { if (value.trim() === oldValue) { changed++; return newValue; } return value; }).join('\n');
    if (changed) { dirty(); render(); syncBulkValues(); }
    status.textContent = changed ? `عُدّل ${changed} موضعًا. تحقق من التعديلات قبل الاعتماد.` : 'لم توجد مواضع مطابقة للاسم المحدد.';
  });
  syncBulkValues();
  render();
  form.addEventListener('submit', event => {
    if (rows.length > maxRows) { event.preventDefault(); status.textContent = `الحد الأقصى ${maxRows} حصة لكل معاينة.`; return; }
    form.querySelector('[data-draft-rows]').value = JSON.stringify(rows.map(data => Object.fromEntries([...fields, ...metadataFields].map(field => [field, data[field] || '']))));
  });
  const finalForm = root.querySelector('[data-import-confirmation] form');
  if (finalForm) {
    let saving = false;
    finalForm.addEventListener('submit', event => {
      const button = root.querySelector('[data-final-import]');
      if (saving || button.disabled) { event.preventDefault(); return; }
      saving = true;
      button.disabled = true;
      button.textContent = 'جارٍ حفظ البيانات…';
    });
  }
})();
