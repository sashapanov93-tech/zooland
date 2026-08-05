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
function updateBreeds() {
  if (!typeField || !breedList || !window.zooBreeds) return;
  breedList.replaceChildren(...(window.zooBreeds[typeField.value] || []).map((breed) => {
    const option = document.createElement('option'); option.value = breed; return option;
  }));
}
if (typeField) { typeField.addEventListener('change', updateBreeds); updateBreeds(); }

document.querySelectorAll('.publish-kind').forEach((tabs) => {
  const buttons = tabs.querySelectorAll('.kind-tab');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const kind = button.dataset.kind;
      buttons.forEach((btn) => btn.classList.toggle('active', btn === button));
      document.querySelectorAll('.kind-form').forEach((form) => {
        form.hidden = form.dataset.kindForm !== kind;
      });
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
