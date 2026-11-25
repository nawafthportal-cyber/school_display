document.addEventListener('DOMContentLoaded', () => {
    const body = document.body;
    const header = document.getElementById('header');
    const menuToggle = document.querySelector('[data-menu-toggle]');
    const mobileMenu = document.getElementById('mobileMenu');

    function updateScrollState() {
        header?.classList.toggle('scrolled', window.scrollY > 18);
    }

    function setMenu(open) {
        if (!menuToggle || !mobileMenu) return;
        menuToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        menuToggle.setAttribute('aria-label', open ? 'إغلاق القائمة' : 'فتح القائمة');
        menuToggle.querySelector('i').className = open ? 'fa-solid fa-xmark' : 'fa-solid fa-bars';
        mobileMenu.hidden = !open;
    }

    window.addEventListener('scroll', updateScrollState, { passive: true });
    updateScrollState();

    menuToggle?.addEventListener('click', () => {
        setMenu(menuToggle.getAttribute('aria-expanded') !== 'true');
    });

    mobileMenu?.querySelectorAll('a').forEach((link) => {
        link.addEventListener('click', () => setMenu(false));
    });

    const showcase = document.querySelector('[data-product-showcase]');
    if (showcase) {
        const tabList = showcase.querySelector('[role="tablist"]');
        const tabs = Array.from(tabList.querySelectorAll('[data-shot-target]'));
        const shots = Array.from(showcase.querySelectorAll('[data-shot]'));

        function selectShot(target) {
            tabs.forEach((tab) => {
                const selected = tab.dataset.shotTarget === target;
                tab.classList.toggle('active', selected);
                tab.setAttribute('aria-selected', String(selected));
                tab.tabIndex = selected ? 0 : -1;
            });
            shots.forEach((shot) => {
                const selected = shot.dataset.shot === target;
                shot.hidden = !selected;
                shot.classList.toggle('active', selected);
            });
        }

        tabs.forEach((tab) => {
            tab.addEventListener('click', () => selectShot(tab.dataset.shotTarget));
        });
        tabList.addEventListener('keydown', (event) => {
            const currentIndex = tabs.indexOf(event.target);
            if (currentIndex < 0) return;
            const nextIndex = {
                Home: 0,
                End: tabs.length - 1,
                ArrowLeft: (currentIndex + 1) % tabs.length,
                ArrowRight: (currentIndex + tabs.length - 1) % tabs.length,
            }[event.key];
            if (!Number.isInteger(nextIndex)) return;
            event.preventDefault();
            selectShot(tabs[nextIndex].dataset.shotTarget);
            tabs[nextIndex].focus();
        });
        tabList.hidden = false;
    }

    const pricingPicker = document.querySelector('[data-pricing-picker]');
    const pricingGroups = document.querySelector('[data-pricing-groups]');
    if (pricingPicker && pricingGroups) {
        const choices = Array.from(pricingPicker.querySelectorAll('[data-pricing-choice]'));
        const groups = Array.from(pricingGroups.querySelectorAll('.pricing-group'));

        choices.forEach((choice) => {
            choice.addEventListener('click', () => {
                const index = Number(choice.dataset.pricingChoice);
                choices.forEach((item) => item.setAttribute('aria-pressed', String(item === choice)));
                groups.forEach((group, groupIndex) => {
                    group.hidden = groupIndex !== index;
                });
                groups[index].open = true;
            });
        });
        choices[0].click();
        pricingGroups.dataset.enhanced = '';
        pricingPicker.hidden = false;
    }

    const overviewVideo = document.getElementById('schoolOverviewVideo');
    const videoPlay = document.querySelector('[data-video-play]');
    const videoChapters = document.querySelector('[data-video-chapters]');
    if (overviewVideo && videoPlay && videoChapters) {
        let pendingSeek = null;

        overviewVideo.addEventListener('loadedmetadata', () => {
            if (pendingSeek === null) return;
            overviewVideo.currentTime = pendingSeek;
            pendingSeek = null;
        });
        overviewVideo.addEventListener('play', () => {
            if (document.activeElement === videoPlay) overviewVideo.focus();
            videoPlay.hidden = true;
        });

        function playOverview() {
            // Native controls stay available when playback is blocked by the browser.
            overviewVideo.play().catch(() => overviewVideo.focus());
        }

        videoPlay.addEventListener('click', playOverview);
        videoChapters.querySelectorAll('[data-video-seek]').forEach((chapter) => {
            chapter.addEventListener('click', () => {
                const start = Number(chapter.dataset.videoSeek);
                if (overviewVideo.readyState >= HTMLMediaElement.HAVE_METADATA) {
                    overviewVideo.currentTime = start;
                } else {
                    pendingSeek = start;
                }
                playOverview();
            });
        });

        videoPlay.hidden = false;
        videoChapters.hidden = false;
    }

    const lightbox = document.getElementById('imageLightbox');
    const lightboxImage = document.getElementById('lightboxImage');
    const lightboxCaption = document.getElementById('lightboxCaption');
    const lightboxViewport = lightbox?.querySelector('.lightbox-viewport');
    const lightboxSize = lightbox?.querySelector('[data-lightbox-size]');
    let lastLightboxTrigger = null;

    lightboxSize?.addEventListener('click', () => {
        const zoomed = lightboxViewport.classList.toggle('is-zoomed');
        lightboxSize.setAttribute('aria-pressed', String(zoomed));
        lightboxSize.textContent = zoomed ? 'عرض كامل الشاشة' : 'تكبير التفاصيل';
    });

    function closeLightbox() {
        if (!lightbox) return;
        lightbox.classList.remove('active');
        lightbox.setAttribute('aria-hidden', 'true');
        lightbox.setAttribute('inert', '');
        lightboxViewport.classList.remove('is-zoomed');
        lightboxViewport.scrollTo(0, 0);
        lightboxSize.setAttribute('aria-pressed', 'false');
        lightboxSize.textContent = 'تكبير التفاصيل';
        body.classList.remove('modal-open');
        lastLightboxTrigger?.focus();
    }

    document.querySelectorAll('[data-zoom-image]').forEach((button) => {
        button.addEventListener('click', () => {
            const image = button.querySelector('img');
            const caption = button.closest('figure')?.querySelector('figcaption')?.innerText || '';
            if (!image || !lightbox || !lightboxImage) return;
            lastLightboxTrigger = button;
            lightboxImage.src = image.currentSrc || image.src;
            lightboxImage.alt = image.alt;
            if (lightboxCaption) lightboxCaption.textContent = caption.replace(/\s+/g, ' ').trim();
            lightbox.classList.add('active');
            lightbox.removeAttribute('inert');
            lightbox.setAttribute('aria-hidden', 'false');
            body.classList.add('modal-open');
            lightbox.querySelector('[data-close-lightbox]')?.focus();
        });
    });

    lightbox?.querySelector('[data-close-lightbox]')?.addEventListener('click', closeLightbox);
    lightbox?.addEventListener('click', (event) => {
        if (event.target === lightbox) closeLightbox();
    });

    const modal = document.getElementById('orderModal');
    const modalCard = modal?.querySelector('.modal-card');
    const orderForm = document.getElementById('orderForm');
    const formAlert = document.getElementById('formAlert');
    const formSteps = Array.from(document.querySelectorAll('[data-form-step]'));
    const formStepIndicators = Array.from(document.querySelectorAll('[data-step-indicator]'));
    const formNextBtn = document.getElementById('formNextBtn');
    const formBackBtn = document.getElementById('formBackBtn');
    const submitOrderBtn = document.getElementById('submitOrderBtn');
    const submitOrderText = document.getElementById('submitOrderText');
    const trialResult = document.getElementById('trialResult');
    const trialResultTitle = document.getElementById('trialResultTitle');
    const trialLoginMobile = document.getElementById('trialLoginMobile');
    const trialLoginUsername = document.getElementById('trialLoginUsername');
    const trialDashboardLink = document.getElementById('trialDashboardLink');
    const copyTrialUsername = document.getElementById('copyTrialUsername');
    const accountLoginChoice = document.getElementById('accountLoginChoice');
    const orderLoginLink = document.getElementById('orderLoginLink');
    const orderPlanSummary = document.getElementById('orderPlanSummary');
    const formConsentCopy = document.getElementById('formConsentCopy');
    let activeOrderIsTrial = true;
    let lastModalTrigger = null;

    function setFormStep(stepNumber) {
        formSteps.forEach((step) => {
            step.hidden = Number(step.dataset.formStep) !== stepNumber;
        });
        formStepIndicators.forEach((indicator) => {
            indicator.classList.toggle('active', Number(indicator.dataset.stepIndicator) === stepNumber);
        });
    }

    function setPasswordFieldsRequired(required) {
        ['password', 'passwordConfirm'].forEach((id) => {
            const field = document.getElementById(id);
            if (!field) return;
            field.disabled = !required;
            field.required = required;
        });
    }

    function clearFormFeedback() {
        if (formAlert) {
            formAlert.className = 'form-alert';
            formAlert.textContent = '';
        }
        if (trialResult) trialResult.hidden = true;
        orderForm?.classList.remove('trial-complete');
        formSteps.forEach((step) => { step.hidden = false; });
        submitOrderBtn.hidden = false;
        orderForm?.querySelectorAll('.form-group').forEach((group) => group.removeAttribute('hidden'));
        orderForm?.querySelectorAll('[aria-invalid="true"]').forEach((field) => field.removeAttribute('aria-invalid'));
        document.querySelectorAll('[data-error-for]').forEach((error) => {
            error.textContent = '';
            error.closest('.form-group')?.classList.remove('has-error');
        });
    }

    function openOrderModal(button, isTrial) {
        if (!modal || !orderForm) return;
        const { plan, period, term, price, priceDisplay, planCode, orderUrl, userAuthenticated } = button.dataset;
        if (!isTrial && userAuthenticated === 'true' && orderUrl) {
            window.location.assign(orderUrl);
            return;
        }

        lastModalTrigger = button;
        clearFormFeedback();
        setFormStep(1);
        activeOrderIsTrial = isTrial;
        setSubmitting(submitOrderBtn.disabled);
        document.getElementById('p_plan_code').value = isTrial ? '' : planCode;
        document.getElementById('p_plan').value = plan;
        document.getElementById('p_period').value = term || period;
        document.getElementById('p_price').value = price;
        setPasswordFieldsRequired(true);

        document.getElementById('modalTitle').textContent = isTrial ? 'طلب تجربة مجانية' : 'إنشاء حساب ومتابعة الباقة';
        document.getElementById('modalSubtitle').textContent = isTrial
            ? `شغّل شاشة واحدة مجانًا لمدة ${body.dataset.trialDays} يومًا دون بطاقة بنكية.`
            : 'أنشئ حساب المدرسة، ثم أكّد بريدك واختر وسيلة الدفع من صفحة اشتراكك.';
        orderPlanSummary.hidden = isTrial;
        orderPlanSummary.textContent = isTrial ? '' : `${plan} · ${period}${term !== period ? ` (${term})` : ''} · ${priceDisplay}\u00a0ر.س`;
        formConsentCopy.textContent = isTrial
            ? 'بياناتك مخصصة للحساب والدعم فقط.'
            : 'تراجع تفاصيل الباقة والسعر قبل الدفع.';
        accountLoginChoice.hidden = isTrial;
        if (!isTrial && orderLoginLink) {
            orderLoginLink.href = `${body.dataset.loginUrl}?next=${encodeURIComponent(orderUrl)}`;
        }

        modal.classList.add('active');
        modal.removeAttribute('inert');
        modal.setAttribute('aria-hidden', 'false');
        body.classList.add('modal-open');
        window.setTimeout(() => document.getElementById('schoolName')?.focus(), 0);
    }

    function closeOrderModal() {
        if (!modal) return;
        modal.classList.remove('active');
        modal.setAttribute('aria-hidden', 'true');
        modal.setAttribute('inert', '');
        body.classList.remove('modal-open');
        lastModalTrigger?.focus();
    }

    function validateFirstStep() {
        const fields = ['schoolName', 'mobile', 'email'].map((id) => document.getElementById(id));
        for (const field of fields) {
            if (field && !field.checkValidity()) {
                field.reportValidity();
                field.focus();
                return false;
            }
        }
        return true;
    }

    formNextBtn?.addEventListener('click', () => {
        if (!validateFirstStep()) return;
        setFormStep(2);
        window.setTimeout(() => document.getElementById('city')?.focus(), 0);
    });

    formBackBtn?.addEventListener('click', () => {
        setFormStep(1);
        window.setTimeout(() => document.getElementById('schoolName')?.focus(), 0);
    });

    formSteps[0]?.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();
            formNextBtn?.click();
        }
    });

    function showFormAlert(type, message) {
        if (!formAlert) return;
        formAlert.className = `form-alert active ${type}`;
        formAlert.setAttribute('role', type === 'error' ? 'alert' : 'status');
        formAlert.textContent = message;
    }

    function showFieldErrors(errors) {
        const firstField = Object.keys(errors).find((field) => field !== '__all__');
        if (firstField) {
            const errorElement = document.querySelector(`[data-error-for="${firstField}"]`);
            const errorStep = errorElement?.closest('[data-form-step]');
            if (errorStep) setFormStep(Number(errorStep.dataset.formStep));
        }
        Object.entries(errors).forEach(([field, message]) => {
            if (field === '__all__') return;
            const errorElement = document.querySelector(`[data-error-for="${field}"]`);
            const fieldElement = orderForm?.elements.namedItem(field);
            if (errorElement) {
                errorElement.textContent = message;
                errorElement.closest('.form-group')?.classList.add('has-error');
            }
            if (fieldElement instanceof HTMLElement) fieldElement.setAttribute('aria-invalid', 'true');
        });
        showFormAlert('error', errors.__all__ || 'راجع الحقول المحددة ثم أعد المحاولة.');
        orderForm?.querySelector('[aria-invalid="true"]')?.focus();
    }

    function setSubmitting(submitting) {
        if (!submitOrderBtn || !submitOrderText) return;
        submitOrderBtn.disabled = submitting;
        submitOrderText.textContent = submitting ? 'جاري تجهيز الحساب...' : (activeOrderIsTrial ? 'إنشاء التجربة الآن' : 'إنشاء الحساب والمتابعة');
        const icon = submitOrderBtn.querySelector('i');
        if (icon) icon.className = submitting ? 'fa-solid fa-circle-notch fa-spin' : 'fa-solid fa-wand-magic-sparkles';
    }

    function showTrialSuccess(payload) {
        const username = payload.username || '';
        const mobile = payload.mobile || document.getElementById('mobile')?.value || '';
        accountLoginChoice.hidden = true;
        trialLoginUsername.textContent = username || '—';
        trialLoginMobile.textContent = mobile || '—';
        trialDashboardLink.href = payload.redirect_url || body.dataset.dashboardUrl;
        trialResultTitle.textContent = activeOrderIsTrial ? 'تم إنشاء تجربة المدرسة بنجاح' : 'تم إنشاء الحساب وتجهيز المتابعة';
        trialDashboardLink.textContent = activeOrderIsTrial ? 'الدخول إلى لوحة التحكم' : 'اختيار الباقة وإكمال الدفع';
        trialResult.hidden = false;
        orderForm.classList.add('trial-complete');
        formSteps.forEach((step) => { step.hidden = true; });
        submitOrderBtn.hidden = true;
        showFormAlert('success', 'تم تجهيز الحساب. احتفظ ببيانات الدخول الظاهرة هنا.');
        trialResult.focus();
        if (!activeOrderIsTrial && payload.redirect_url) {
            window.setTimeout(() => window.location.assign(payload.redirect_url), 700);
        }
    }

    async function submitTrialSignup() {
        clearFormFeedback();
        setSubmitting(true);
        try {
            const response = await fetch(orderForm.action, {
                method: 'POST',
                body: new FormData(orderForm),
                headers: { 'X-Requested-With': 'XMLHttpRequest' }
            });
            const payload = await response.json();
            if (!response.ok || !payload.ok) {
                showFieldErrors(payload.errors || { __all__: 'تعذر إنشاء التجربة. حاول مرة أخرى.' });
                return;
            }
            showTrialSuccess(payload);
        } catch (error) {
            showFormAlert('error', 'تعذر الاتصال بالخادم. تحقق من الاتصال ثم أعد المحاولة.');
        } finally {
            setSubmitting(false);
        }
    }

    orderForm?.addEventListener('submit', (event) => {
        event.preventDefault();
        clearFormFeedback();
        if (!orderForm.reportValidity()) return;
        submitTrialSignup();
    });

    copyTrialUsername?.addEventListener('click', async () => {
        const value = trialLoginUsername?.textContent.trim();
        if (!value || value === '—') return;
        try {
            await navigator.clipboard.writeText(value);
            copyTrialUsername.textContent = 'تم النسخ';
        } catch (error) {
            copyTrialUsername.textContent = 'انسخه يدوياً';
        }
    });

    modal?.addEventListener('click', (event) => {
        if (event.target === modal) closeOrderModal();
    });

    document.addEventListener('click', (event) => {
        const planButton = event.target.closest('[data-role="open-plan"]');
        if (planButton) {
            openOrderModal(planButton, false);
            return;
        }
        const trialButton = event.target.closest('[data-role="open-trial"]');
        if (trialButton) {
            openOrderModal(trialButton, true);
            return;
        }
        if (event.target.closest('[data-role="close-order-modal"]')) closeOrderModal();
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            if (modal?.classList.contains('active')) closeOrderModal();
            else if (lightbox?.classList.contains('active')) closeLightbox();
            else setMenu(false);
        }
        if (event.key !== 'Tab') return;
        const activeDialog = modal?.classList.contains('active') ? modalCard : (lightbox?.classList.contains('active') ? lightbox.querySelector('.lightbox-shell') : null);
        if (!activeDialog) return;
        const focusable = Array.from(activeDialog.querySelectorAll('button:not([disabled]), a[href], input:not([disabled]), [tabindex]:not([tabindex="-1"])')).filter((element) => !element.hidden && element.offsetParent !== null);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    });
});
