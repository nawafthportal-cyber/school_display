const subscriptionPage = document.querySelector('[data-subscription-page]');

if (subscriptionPage) {
    const moneyFormat = new Intl.NumberFormat('ar-SA', { maximumFractionDigits: 2 });
    const formatMoney = (value) => `${moneyFormat.format(Number(value))} ر.س`;
    const tabs = Array.from(subscriptionPage.querySelectorAll('[data-subscription-tab]'));

    function selectTab(selected) {
        for (const tab of tabs) {
            const active = tab === selected;
            tab.setAttribute('aria-selected', String(active));
            tab.tabIndex = active ? 0 : -1;
            document.getElementById(tab.getAttribute('aria-controls')).hidden = !active;
        }
    }

    for (const tab of tabs) {
        tab.addEventListener('click', () => selectTab(tab));
        tab.addEventListener('keydown', (event) => {
            const current = tabs.indexOf(tab);
            const next = {
                ArrowLeft: (current + 1) % tabs.length,
                ArrowRight: (current + tabs.length - 1) % tabs.length,
                Home: 0,
                End: tabs.length - 1,
            }[event.key];
            if (next === undefined) return;
            event.preventDefault();
            selectTab(tabs[next]);
            tabs[next].focus();
        });
    }
    if (tabs.length) {
        selectTab(tabs.find((tab) => tab.dataset.subscriptionTab === subscriptionPage.dataset.initialTab) || tabs[0]);
    }

    function selectPaymentMethod(hub, selected) {
        for (const button of hub.querySelectorAll('[data-payment-choice]')) {
            button.setAttribute('aria-pressed', String(button === selected));
        }
        for (const panel of hub.querySelectorAll('[data-payment-panel]')) {
            panel.hidden = panel.dataset.paymentMethod !== selected.dataset.paymentMethod;
        }
    }

    for (const hub of subscriptionPage.querySelectorAll('[data-payment-hub]')) {
        const choices = Array.from(hub.querySelectorAll('[data-payment-choice]:not(:disabled)'));
        for (const choice of choices) {
            choice.addEventListener('click', () => selectPaymentMethod(hub, choice));
        }
        const defaultChoice = choices.find((choice) => choice.dataset.paymentMethod === hub.dataset.defaultMethod);
        if (defaultChoice || choices[0]) selectPaymentMethod(hub, defaultChoice || choices[0]);
    }

    const planMap = JSON.parse(document.getElementById('plans-map').textContent);
    const checkout = document.getElementById('new-payment-hub');
    const prompt = document.getElementById('payment-plan-prompt');

    function showSelectedPlan() {
        const chosen = subscriptionPage.querySelector('[data-plan-option]:checked');
        const plan = planMap[chosen?.dataset.planId];
        checkout.hidden = !plan;
        prompt.hidden = Boolean(plan);
        for (const input of checkout.querySelectorAll('[data-checkout-plan]')) {
            input.value = plan ? plan.id : '';
        }
        if (!plan) return;
        checkout.querySelector('[data-paid-checkout]').hidden = plan.is_free;
        checkout.querySelector('[data-free-checkout]').hidden = !plan.is_free;
        checkout.querySelector('[data-checkout-heading]').textContent = plan.is_free ? 'طلب الباقة المجانية' : 'وسيلة الدفع';
        checkout.querySelector('[data-checkout-summary]').textContent = `${plan.name} · ${plan.duration_label}`;
        checkout.querySelector('[data-checkout-total]').textContent = plan.is_free ? 'مجانًا' : formatMoney(plan.price);
    }

    for (const option of subscriptionPage.querySelectorAll('[data-plan-option]')) {
        option.addEventListener('change', showSelectedPlan);
    }
    window.addEventListener('pageshow', showSelectedPlan);
    showSelectedPlan();

    const addon = document.getElementById('screen-addon-purchase');
    if (addon) {
        const matrix = JSON.parse(document.getElementById('screen-addon-price-matrix').textContent);
        const quantity = addon.querySelector('[data-screen-addon-count]');
        const duration = addon.querySelector('[data-screen-addon-duration]');
        const total = addon.querySelector('[data-screen-addon-total]');
        function updateAddonAmount() {
            const cost = matrix[duration.value]?.[quantity.valueAsNumber];
            total.textContent = cost === undefined ? '—' : formatMoney(cost);
        }
        function revealAddonOnArrival() {
            if (window.location.hash === '#screen-addon-purchase') addon.open = true;
        }
        quantity.addEventListener('input', updateAddonAmount);
        duration.addEventListener('change', updateAddonAmount);
        window.addEventListener('hashchange', revealAddonOnArrival);
        revealAddonOnArrival();
    }

    for (const button of subscriptionPage.querySelectorAll('[data-copy]')) {
        button.addEventListener('click', async () => {
            const label = button.querySelector('.copy-text');
            const original = label.textContent;
            try {
                await navigator.clipboard.writeText(button.dataset.copy);
                label.textContent = 'تم النسخ';
            } catch {
                label.textContent = 'انسخه يدويًا';
            }
            window.setTimeout(() => { label.textContent = original; }, 2000);
        });
    }
}
