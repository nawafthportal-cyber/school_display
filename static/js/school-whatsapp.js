(() => {
  const root = document.getElementById('schoolWhatsapp');
  if (!root) return;
  const status = root.querySelector('#id_subscription_status');
  const search = root.querySelector('#whatsappSchoolSearch');
  const message = root.querySelector('#whatsappMessage');
  const cards = [...root.querySelectorAll('[data-wa-school]')];
  const links = [...root.querySelectorAll('[data-wa-phone]')];

  const filter = () => {
    const query = search.value.trim().toLocaleLowerCase('ar');
    cards.forEach(card => {
      const matches = status.value === 'active' ? card.dataset.subscriptionActive === '1'
        : status.value === 'inactive' ? card.dataset.subscriptionActive === '0'
        : status.value === 'expired_trial' ? card.dataset.expiredTrial === '1' : true;
      card.hidden = !matches || (query !== '' && !card.textContent.toLocaleLowerCase('ar').includes(query));
    });
    const visible = cards.filter(card => !card.hidden);
    const numbers = new Set(visible.flatMap(card => [...card.querySelectorAll('[data-wa-phone]')].map(link => link.dataset.waPhone)));
    root.querySelector('#whatsappSummary').textContent = `المدارس الظاهرة: ${visible.length} — أرقام التواصل المتاحة: ${numbers.size}`;
    root.querySelector('#whatsappEmpty').hidden = visible.length > 0;
  };

  const prepareLinks = () => {
    const text = message.value.trim();
    links.forEach(link => {
      link.setAttribute('aria-disabled', text ? 'false' : 'true');
      if (text) {
        link.href = `https://wa.me/${link.dataset.waPhone}?text=${encodeURIComponent(text)}`;
        link.removeAttribute('tabindex');
      } else {
        link.removeAttribute('href');
        link.tabIndex = -1;
      }
      link.closest('.wa-contact').querySelector('[data-wa-opened]').hidden = true;
    });
  };

  links.forEach(link => link.addEventListener('click', event => {
    if (!message.value.trim()) {
      event.preventDefault();
      message.focus();
      return;
    }
    // Opening a chat is not evidence of delivery; the user sends in WhatsApp.
    links.filter(other => other.dataset.waPhone === link.dataset.waPhone).forEach(other => {
      other.closest('.wa-contact').querySelector('[data-wa-opened]').hidden = false;
    });
  }));
  status.addEventListener('change', filter);
  search.addEventListener('input', filter);
  message.addEventListener('input', prepareLinks);
  prepareLinks();
  filter();
})();
