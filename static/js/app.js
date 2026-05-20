(function () {

    const navToggle = document.querySelector('[data-nav-toggle]');
    const nav = document.querySelector('[data-nav]');
    if (navToggle && nav) {
        navToggle.addEventListener('click', () => nav.classList.toggle('is-open'));
    }

    const roleSelect = document.querySelector('[data-role-select]');
    const workerFields = document.querySelector('[data-worker-fields]');
    function toggleWorkerFields() {
        if (!roleSelect || !workerFields) return;
        workerFields.classList.toggle('is-active', roleSelect.value === 'worker');
    }
    if (roleSelect) {
        roleSelect.addEventListener('change', toggleWorkerFields);
        toggleWorkerFields();
    }

    const orderForm = document.querySelector('[data-order-form]');
    if (!orderForm) return;

    const categorySelect = orderForm.querySelector('[data-category-select]');
    const serviceSelect = orderForm.querySelector('[data-service-select]');
    const sections = Array.from(orderForm.querySelectorAll('[data-category-section]'));

    function selectedCategorySlug() {
        const selected = categorySelect.options[categorySelect.selectedIndex];
        return selected ? selected.dataset.slug : '';
    }

    function filterServices() {
        const categoryId = categorySelect.value;
        Array.from(serviceSelect.options).forEach((option) => {
            if (!option.value) return;
            option.hidden = option.dataset.category !== categoryId;
        });
        const current = serviceSelect.options[serviceSelect.selectedIndex];
        if (current && current.value && current.dataset.category !== categoryId) {
            serviceSelect.value = '';
        }
        toggleSections();
    }

    function toggleSections() {
        const slug = selectedCategorySlug();
        sections.forEach((section) => {
            section.classList.toggle('is-active', section.dataset.categorySection === slug);
        });
    }

    if (categorySelect && serviceSelect) {
        categorySelect.addEventListener('change', filterServices);
        serviceSelect.addEventListener('change', toggleSections);
        filterServices();
    }

    const priceButton = orderForm.querySelector('[data-price-button]');
    const priceOutput = orderForm.querySelector('[data-price-output]');
    const priceDetails = orderForm.querySelector('[data-price-details]');
    if (priceButton && priceOutput) {
        priceButton.addEventListener('click', async () => {
            priceOutput.textContent = 'рахуємо...';
            if (priceDetails) priceDetails.innerHTML = '';
            try {
                const response = await fetch(orderForm.dataset.priceUrl, {
                    method: 'POST',
                    body: new FormData(orderForm)
                });
                const data = await response.json();
                if (!response.ok || !data.ok) {
                    priceOutput.textContent = (data.errors || ['Не вдалося розрахувати ціну.']).join(' ');
                    return;
                }
                priceOutput.textContent = data.price_text;
                if (priceDetails && Array.isArray(data.details)) {
                    priceDetails.innerHTML = data.details.map(item => `<li>${item}</li>`).join('');
                }
            } catch (error) {
                priceOutput.textContent = 'Помилка з’єднання. Спробуйте ще раз.';
            }
        });
    }
})();
