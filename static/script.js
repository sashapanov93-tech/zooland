// CSRF-защита для обычных форм и AJAX-запросов. Значение выдаёт сервер
// в cookie, а сюда добавляется только для изменяющих состояние запросов.
const readCookie = (name) => {
  const prefix = `${encodeURIComponent(name)}=`;
  return document.cookie.split(';').map((value) => value.trim()).reduce((found, value) => {
    if (found || !value.startsWith(prefix)) return found;
    try {
      return decodeURIComponent(value.slice(prefix.length));
    } catch (_error) {
      return '';
    }
  }, '');
};

// На каждой внутренней странице показываем одинаковую отдельную кнопку назад.
// Текстовые ссылки шаблона могут вести в конкретный раздел и её не заменяют.
const siteHeaderInner = document.querySelector('.site-header .header-inner');
if (siteHeaderInner && window.location.pathname !== '/') {
  const backButton = document.createElement('button');
  backButton.type = 'button';
  backButton.className = 'history-back-button';
  backButton.textContent = '←';
  backButton.title = 'Вернуться в предыдущий раздел';
  backButton.setAttribute('aria-label', 'Вернуться в предыдущий раздел');
  backButton.addEventListener('click', () => {
    const previousPage = document.referrer ? new URL(document.referrer, window.location.href) : null;
    if (previousPage?.origin === window.location.origin && window.history.length > 1) {
      window.history.back();
    } else {
      window.location.assign('/');
    }
  });
  siteHeaderInner.querySelector('.brand')?.after(backButton);
}

const csrfToken = readCookie('zooland_csrf');
const csrfMethods = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
const formChangesState = (form) => csrfMethods.has((form.getAttribute('method') || 'GET').toUpperCase());

const addCsrfTokenToForm = (form) => {
  if (!csrfToken || !formChangesState(form)) return;
  let input = form.querySelector('input[name="csrf_token"]');
  if (!input) {
    input = document.createElement('input');
    input.type = 'hidden';
    input.name = 'csrf_token';
    form.append(input);
  }
  input.value = csrfToken;
};

document.querySelectorAll('form').forEach(addCsrfTokenToForm);
// Заодно защищаем формы, которые могут появиться на странице позднее.
document.addEventListener('submit', (event) => addCsrfTokenToForm(event.target), true);

const withCsrfHeader = (headers = {}) => (
  csrfToken ? { ...headers, 'X-CSRF-Token': csrfToken } : headers
);

// Поведение интерфейса живёт во внешнем скрипте: это позволяет production CSP
// полностью запретить inline JavaScript и обработчики в HTML.
document.querySelectorAll('form[data-confirm]').forEach((form) => {
  form.addEventListener('submit', (event) => {
    if (!window.confirm(form.dataset.confirm || 'Подтвердить действие?')) {
      event.preventDefault();
    }
  });
});

document.querySelectorAll('[data-submit-on-change]').forEach((field) => {
  field.addEventListener('change', () => field.form?.requestSubmit());
});

const activateGalleryThumb = (thumb) => {
  const gallery = thumb.closest('.gallery');
  const mainImage = gallery?.querySelector('.gallery-main');
  if (!mainImage) return;
  mainImage.src = thumb.src;
  gallery.querySelectorAll('.gallery-thumb').forEach((item) => item.classList.remove('active'));
  thumb.classList.add('active');
};
document.querySelectorAll('.gallery-thumb').forEach((thumb) => {
  thumb.addEventListener('click', () => activateGalleryThumb(thumb));
  thumb.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    activateGalleryThumb(thumb);
  });
});

document.querySelectorAll('.reveal-call').forEach((button) => {
  button.addEventListener('click', () => {
    const phone = button.closest('.owner-card')?.querySelector('.reveal-phone');
    const number = phone?.dataset.phone;
    if (!phone || !number) return;
    phone.textContent = number;
    phone.classList.add('revealed');
    const link = document.createElement('a');
    link.className = 'contact-link detail-call revealed-call';
    link.href = `tel:${number.replace(/[^+\d]/g, '')}`;
    link.textContent = 'Позвонить';
    for (const key of ['listingMetric', 'listingType', 'listingId']) {
      if (button.dataset[key]) link.dataset[key] = button.dataset[key];
    }
    button.replaceWith(link);
  });
});

const threadMessages = document.querySelector('.thread-messages');
if (threadMessages) threadMessages.scrollTop = threadMessages.scrollHeight;

// Контактная статистика остаётся честной и приватной: сервер получает только
// тип карточки и действие, без номера телефона, текста, IP или профиля гостя.
// Переход не ждёт аналитику — звонок/Telegram/чат остаются рабочими даже при
// временной ошибке сети.
document.addEventListener('click', (event) => {
  const control = event.target.closest?.('[data-listing-metric]');
  if (!control || control.hasAttribute('disabled')) return;
  const { listingMetric: metric, listingType: listingType, listingId: listingId } = control.dataset;
  if (!metric || !listingType || !/^\d+$/.test(listingId || '')) return;
  fetch(`/listing/${encodeURIComponent(listingType)}/${encodeURIComponent(listingId)}/metric/${encodeURIComponent(metric)}`, {
    method: 'POST',
    headers: withCsrfHeader(),
    credentials: 'same-origin',
    keepalive: true,
  }).catch(() => {});
}, true);

// Показывает пароль только по явному нажатию пользователя. Работает во всех
// формах: вход, регистрация, восстановление, смена email и удаление аккаунта.
document.querySelectorAll('input[type="password"]').forEach((input, index) => {
  if (input.dataset.passwordToggleReady) return;
  input.dataset.passwordToggleReady = 'true';
  if (!input.id) input.id = `zooland-password-${index + 1}`;

  const field = document.createElement('span');
  field.className = 'password-field';
  input.parentNode.insertBefore(field, input);
  field.append(input);

  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'password-toggle';
  toggle.setAttribute('aria-controls', input.id);
  toggle.setAttribute('aria-pressed', 'false');
  toggle.textContent = 'Показать';
  toggle.addEventListener('click', () => {
    const isHidden = input.type === 'password';
    input.type = isHidden ? 'text' : 'password';
    toggle.textContent = isHidden ? 'Скрыть' : 'Показать';
    toggle.setAttribute('aria-pressed', String(isHidden));
    toggle.setAttribute('aria-label', isHidden ? 'Скрыть пароль' : 'Показать пароль');
  });
  field.append(toggle);
});

document.querySelectorAll('.menu-btn').forEach((button) => {
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    const menu = button.closest('.card-menu');
    document.querySelectorAll('.card-menu.active').forEach((item) => {
      if (item !== menu) item.classList.remove('active');
    });
    menu.classList.toggle('active');
  });
});

document.addEventListener('click', () => {
  document.querySelectorAll('.card-menu.active').forEach((menu) => menu.classList.remove('active'));
});

const typeField = document.querySelector('#animal-type');
const breedList = document.querySelector('#breeds');
const breedData = document.querySelector('#zoo-breeds-data');
let zooBreeds = {};
try {
  zooBreeds = JSON.parse(breedData?.textContent || '{}');
} catch (error) {
  console.error('Не удалось загрузить список пород.', error);
}
function updateBreeds() {
  if (!typeField || !breedList) return;
  breedList.replaceChildren(...(zooBreeds[typeField.value] || []).map((breed) => {
    const option = document.createElement('option'); option.value = breed; return option;
  }));
}
if (typeField) { typeField.addEventListener('change', updateBreeds); updateBreeds(); }

// У публикации есть несколько обязательных полей. Вместо неочевидного перехода
// браузера к одному полю показываем все незаполненные поля и объясняем причину.
const publishRequiredFields = (form) => Array.from(form.querySelectorAll('[required]'))
  .filter((field) => !field.disabled && field.type !== 'hidden');

const publishFieldLabel = (field) => {
  const explicitLabel = field.getAttribute('aria-label') || field.dataset.fieldLabel;
  if (explicitLabel) return explicitLabel;
  if (field.labels?.length) return field.labels[0].textContent.trim();
  if (field.placeholder) return field.placeholder.replace(/\s*\(.+\)$/, '');
  const emptyOption = field.querySelector?.('option[value=""]');
  if (emptyOption?.textContent.trim()) return emptyOption.textContent.trim();
  return 'это поле';
};

const setPublishFieldInvalid = (field, invalid) => {
  field.classList.toggle('field-invalid', invalid);
  field.setAttribute('aria-invalid', String(invalid));
  const group = field.closest('.deal-type, .contact-choice, .animal-facts');
  if (invalid && group) group.classList.add('field-group-invalid');
  if (!invalid && group && !group.querySelector('.field-invalid')) {
    group.classList.remove('field-group-invalid');
  }
};

const clearPublishFormErrorIfReady = (form) => {
  const hasInvalidFields = publishRequiredFields(form).some((field) => !field.validity.valid);
  if (hasInvalidFields) return;
  const message = form.querySelector('.publish-validation-message');
  if (message) {
    message.hidden = true;
    message.textContent = '';
  }
  form.classList.remove('has-validation-errors');
};

const validatePublishForm = (form) => {
  const invalidFields = publishRequiredFields(form).filter((field) => !field.validity.valid);
  publishRequiredFields(form).forEach((field) => setPublishFieldInvalid(field, invalidFields.includes(field)));

  if (!invalidFields.length) {
    clearPublishFormErrorIfReady(form);
    return true;
  }

  const firstInvalid = invalidFields[0];
  const message = form.querySelector('.publish-validation-message');
  if (message) {
    const fieldName = publishFieldLabel(firstInvalid);
    message.textContent = invalidFields.length === 1
      ? `Заполните поле «${fieldName}», выделенное красным.`
      : `Заполните обязательные поля, выделенные красным. Сначала: «${fieldName}».`;
    message.hidden = false;
  }
  form.classList.add('has-validation-errors');

  window.requestAnimationFrame(() => {
    firstInvalid.scrollIntoView({ behavior: 'smooth', block: 'center' });
    firstInvalid.focus({ preventScroll: true });
  });
  return false;
};

document.querySelectorAll('.publish-modal .kind-form').forEach((form) => {
  // Без JavaScript остаётся нативная проверка HTML. С JavaScript перехватываем
  // отправку, чтобы подсветить сразу все обязательные поля.
  form.noValidate = true;
  form.addEventListener('submit', (event) => {
    if (!validatePublishForm(form)) event.preventDefault();
  });
  ['input', 'change'].forEach((eventName) => form.addEventListener(eventName, (event) => {
    const field = event.target;
    if (!field.matches?.('[required]')) return;
    if (field.classList.contains('field-invalid') && field.validity.valid) {
      setPublishFieldInvalid(field, false);
    }
    clearPublishFormErrorIfReady(form);
  }));
});

// Сокращаем список городов по мере ввода: варианты появляются уже с первой буквы.
const cityList = document.querySelector('#cities');
const allCities = cityList ? Array.from(cityList.options, (option) => option.value) : [];
document.querySelectorAll('input[list="cities"]').forEach((input) => {
  input.addEventListener('input', () => {
    const query = input.value.trim().toLocaleLowerCase('ru-RU');
    const matches = query ? allCities.filter((city) => city.toLocaleLowerCase('ru-RU').startsWith(query)).slice(0, 30) : allCities;
    cityList.replaceChildren(...matches.map((city) => {
      const option = document.createElement('option'); option.value = city; return option;
    }));
  });
});

document.querySelectorAll('.publish-kind').forEach((tabs) => {
  const buttons = tabs.querySelectorAll('.kind-tab');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const kind = button.dataset.kind;
      buttons.forEach((btn) => btn.classList.toggle('active', btn === button));
      document.querySelectorAll('.kind-form').forEach((form) => {
        form.hidden = form.dataset.kindForm !== kind;
      });
      if (button.dataset.deal) {
        const dealInput = tabs.closest('.publish-modal')?.querySelector(`.kind-form[data-kind-form="sale"] input[name="deal_type"][value="${button.dataset.deal}"]`);
        if (dealInput) dealInput.checked = true;
      }
      tabs.closest('.publish-modal')?.querySelector('.kind-form:not([hidden]) select, .kind-form:not([hidden]) input')?.focus();
    });
  });
});

const publishModal = document.querySelector('.publish-modal');
const openPublish = () => {
  if (!publishModal) return;
  publishModal.classList.add('is-open');
  publishModal.setAttribute('aria-hidden', 'false');
  document.body.classList.add('modal-open');
  publishModal.querySelector('select, input')?.focus();
};
const closePublish = () => {
  if (!publishModal) return;
  publishModal.classList.remove('is-open');
  publishModal.setAttribute('aria-hidden', 'true');
  document.body.classList.remove('modal-open');
};
document.querySelectorAll('[data-open-publish]').forEach((button) => button.addEventListener('click', (event) => {
  event.preventDefault(); openPublish();
}));
publishModal?.querySelector('.modal-close')?.addEventListener('click', closePublish);
publishModal?.addEventListener('click', (event) => { if (event.target === publishModal) closePublish(); });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closePublish(); });
if (window.location.hash === '#publish') openPublish();

document.querySelectorAll('[data-ad-format]').forEach((button) => {
  button.addEventListener('click', () => {
    const format = document.querySelector('#advertising-format');
    if (format) format.value = button.dataset.adFormat;
    document.querySelector('#advertising-request')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
});

// Сервер повторно открывает форму после своей проверки (например, когда
// порода не заполнена). Подсвечиваем именно те поля, которые он отклонил.
const serverPublishParams = new URLSearchParams(window.location.search);
const serverPublishErrorNames = new Set([
  ...serverPublishParams.getAll('publish_errors').flatMap((value) => value.split(',')),
  serverPublishParams.get('publish_error') || '',
].map((value) => value.trim()).filter(Boolean));

if (serverPublishErrorNames.size) {
  const saleForm = document.querySelector('.publish-modal .kind-form[data-kind-form="sale"]');
  if (saleForm) {
    document.querySelectorAll('.publish-modal .kind-form').forEach((form) => {
      form.hidden = form !== saleForm;
    });
    document.querySelectorAll('.publish-modal .kind-tab').forEach((tab) => {
      tab.classList.toggle('active', tab.dataset.kind === 'sale' && tab.dataset.deal === 'sale');
    });
    const serverInvalidFields = Array.from(saleForm.elements).filter((field) => (
      field.name && serverPublishErrorNames.has(field.name)
    ));
    serverInvalidFields.forEach((field) => setPublishFieldInvalid(field, true));
    const message = saleForm.querySelector('.publish-validation-message');
    if (message) {
      const firstInvalid = serverInvalidFields[0];
      const fieldName = firstInvalid ? publishFieldLabel(firstInvalid) : 'обязательные поля';
      message.textContent = serverInvalidFields.length > 1
        ? `Не удалось сохранить объявление: проверьте поля, выделенные красным. Сначала: «${fieldName}».`
        : `Не удалось сохранить объявление: проверьте поле «${fieldName}», выделенное красным.`;
      message.hidden = false;
    }
    saleForm.classList.add('has-validation-errors');
    openPublish();
    serverInvalidFields[0]?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    serverInvalidFields[0]?.focus({ preventScroll: true });
  }

  // Ошибки уже показаны: при обновлении страницы они не должны появляться снова.
  const cleanUrl = new URL(window.location.href);
  cleanUrl.searchParams.delete('publish_error');
  cleanUrl.searchParams.delete('publish_errors');
  window.history.replaceState({}, '', `${cleanUrl.pathname}${cleanUrl.search}${cleanUrl.hash}`);
}

document.querySelectorAll('.fav-form').forEach((form) => {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = form.querySelector('.fav-btn');
    if (!button || button.disabled) return;

    button.disabled = true;
    try {
      const response = await fetch(form.action, {
        method: 'POST',
        headers: withCsrfHeader({ 'X-Requested-With': 'XMLHttpRequest' }),
        credentials: 'same-origin',
      });
      if (!response.ok) throw new Error('Не удалось изменить избранное');
      const { favorite } = await response.json();
      button.classList.toggle('active', favorite);
      const label = favorite ? 'Убрать из избранного' : 'В избранное';
      button.title = label;
      button.setAttribute('aria-label', label);
      if (form.classList.contains('detail-fav')) {
        button.textContent = favorite ? '♥ В избранном' : '♥ В избранное';
      }
      if (!favorite && document.body.classList.contains('favorites-page')) {
        form.closest('.card')?.remove();
        if (!document.querySelector('.favorites-page .card')) window.location.reload();
      }
    } catch (_error) {
      // Форма остаётся рабочей и без JavaScript-поддержки сервера.
      form.submit();
    } finally {
      button.disabled = false;
    }
  });
});

// Уведомления о новых сообщениях: включаются только по явному клику пользователя.
const pushButton = document.querySelector('#push-notifications');
if (pushButton && !('Notification' in window)) {
  pushButton.hidden = true;
}
pushButton?.addEventListener('click', async () => {
  const permission = await Notification.requestPermission();
  pushButton.textContent = permission === 'granted'
    ? 'Уведомления о сообщениях включены'
    : 'Браузер не разрешил уведомления';
});

if ('Notification' in window && Notification.permission === 'granted') {
  let firstCheck = true;
  let previousCount = Number(sessionStorage.getItem('zooland-unread-count') || 0);
  const checkUnread = async () => {
    try {
      const response = await fetch('/notifications/unread', {
        headers: withCsrfHeader(),
        credentials: 'same-origin',
      });
      if (!response.ok) return;
      const data = await response.json();
      if (!firstCheck && data.count > previousCount) {
        new Notification('ZooLand: новое сообщение', { body: `${data.sender}: ${data.body}`.slice(0, 180) });
      }
      firstCheck = false;
      previousCount = data.count;
      sessionStorage.setItem('zooland-unread-count', String(data.count));
    } catch (_error) { /* сеть временно недоступна */ }
  };
  checkUnread();
  window.setInterval(checkUnread, 30000);
}

// Переключение объявлений между плиткой и компактным списком.
document.querySelectorAll('.view-switch').forEach((switcher) => {
  const target = document.querySelector(`#${switcher.dataset.viewTarget}`);
  if (!target) return;
  const key = `zooland-view-${switcher.dataset.viewTarget}`;
  const applyView = (view) => {
    target.classList.toggle('view-grid', view === 'grid');
    target.classList.toggle('view-list', view === 'list');
    switcher.querySelectorAll('[data-view]').forEach((button) => button.classList.toggle('active', button.dataset.view === view));
    localStorage.setItem(key, view);
  };
  applyView(localStorage.getItem(key) || (switcher.dataset.viewTarget === 'account-listings' ? 'list' : 'grid'));
  switcher.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => applyView(button.dataset.view)));
});
