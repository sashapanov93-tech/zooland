(() => {
  const input = document.querySelector('#avatar-input');
  const dialog = document.querySelector('#avatar-crop-dialog');
  const canvas = document.querySelector('#avatar-crop-canvas');
  const zoomControl = document.querySelector('#avatar-crop-zoom');
  const applyButton = document.querySelector('#avatar-crop-apply');
  const cancelButton = document.querySelector('#avatar-crop-cancel');
  const closeButton = document.querySelector('#avatar-crop-close');
  const preview = document.querySelector('#avatar-preview');
  const uploadHint = document.querySelector('#avatar-upload-hint');
  const status = document.querySelector('#avatar-crop-status');
  const profileForm = input?.closest('form');

  if (!input || !dialog || !canvas || !zoomControl || !applyButton || !preview) return;

  const context = canvas.getContext('2d', { alpha: false });
  const image = new Image();
  const state = {
    ready: false,
    zoom: 1,
    offsetX: 0,
    offsetY: 0,
    dragging: false,
    startX: 0,
    startY: 0,
    startOffsetX: 0,
    startOffsetY: 0,
  };

  const clampOffsets = () => {
    if (!state.ready) return;
    const baseScale = Math.max(canvas.width / image.naturalWidth, canvas.height / image.naturalHeight);
    const scaledWidth = image.naturalWidth * baseScale * state.zoom;
    const scaledHeight = image.naturalHeight * baseScale * state.zoom;
    const maxX = Math.max(0, (scaledWidth - canvas.width) / 2);
    const maxY = Math.max(0, (scaledHeight - canvas.height) / 2);
    state.offsetX = Math.max(-maxX, Math.min(maxX, state.offsetX));
    state.offsetY = Math.max(-maxY, Math.min(maxY, state.offsetY));
  };

  const draw = () => {
    if (!state.ready) return;
    clampOffsets();
    const baseScale = Math.max(canvas.width / image.naturalWidth, canvas.height / image.naturalHeight);
    const scale = baseScale * state.zoom;
    const width = image.naturalWidth * scale;
    const height = image.naturalHeight * scale;
    const left = (canvas.width - width) / 2 + state.offsetX;
    const top = (canvas.height - height) / 2 + state.offsetY;
    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, left, top, width, height);
  };

  const resetInput = () => {
    input.value = '';
    state.ready = false;
  };

  const closeCropper = (keepFile = false) => {
    if (!keepFile) resetInput();
    if (dialog.open) dialog.close();
  };

  const showError = (message) => {
    status.textContent = message;
    applyButton.disabled = false;
  };

  input.addEventListener('change', () => {
    const file = input.files?.[0];
    if (!file) return;
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
      resetInput();
      uploadHint.textContent = 'Выберите изображение JPG, PNG или WebP.';
      uploadHint.style.color = '#8a3f38';
      return;
    }

    state.ready = false;
    status.textContent = 'Подготавливаем фотографию…';
    applyButton.disabled = true;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');

    const reader = new FileReader();
    reader.onerror = () => {
      showError('Не удалось открыть фотографию. Выберите другой файл.');
    };
    reader.onload = () => {
      image.onerror = () => showError('Файл не удалось распознать как фотографию.');
      image.onload = () => {
        state.ready = true;
        state.zoom = 1;
        state.offsetX = 0;
        state.offsetY = 0;
        zoomControl.value = '1';
        status.textContent = '';
        applyButton.disabled = false;
        draw();
      };
      image.src = String(reader.result || '');
    };
    reader.readAsDataURL(file);
  });

  zoomControl.addEventListener('input', () => {
    state.zoom = Number(zoomControl.value) || 1;
    draw();
  });

  canvas.addEventListener('pointerdown', (event) => {
    if (!state.ready) return;
    state.dragging = true;
    state.startX = event.clientX;
    state.startY = event.clientY;
    state.startOffsetX = state.offsetX;
    state.startOffsetY = state.offsetY;
    canvas.setPointerCapture(event.pointerId);
  });

  canvas.addEventListener('pointermove', (event) => {
    if (!state.dragging) return;
    const bounds = canvas.getBoundingClientRect();
    const ratio = canvas.width / bounds.width;
    state.offsetX = state.startOffsetX + (event.clientX - state.startX) * ratio;
    state.offsetY = state.startOffsetY + (event.clientY - state.startY) * ratio;
    draw();
  });

  const stopDragging = (event) => {
    if (!state.dragging) return;
    state.dragging = false;
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
  };
  canvas.addEventListener('pointerup', stopDragging);
  canvas.addEventListener('pointercancel', stopDragging);

  canvas.addEventListener('keydown', (event) => {
    const directions = {
      ArrowLeft: [-1, 0],
      ArrowRight: [1, 0],
      ArrowUp: [0, -1],
      ArrowDown: [0, 1],
    };
    const direction = directions[event.key];
    if (!direction || !state.ready) return;
    event.preventDefault();
    const distance = event.shiftKey ? 40 : 12;
    state.offsetX += direction[0] * distance;
    state.offsetY += direction[1] * distance;
    draw();
  });

  applyButton.addEventListener('click', () => {
    if (!state.ready) return;
    applyButton.disabled = true;
    status.textContent = 'Сохраняем выбранную область…';
    canvas.toBlob((blob) => {
      if (!blob) {
        showError('Не удалось подготовить аватар. Попробуйте другое фото.');
        return;
      }
      try {
        const croppedFile = new File([blob], 'zooland-avatar.jpg', {
          type: 'image/jpeg',
          lastModified: Date.now(),
        });
        const transfer = new DataTransfer();
        transfer.items.add(croppedFile);
        input.files = transfer.files;
      } catch (_error) {
        showError('Браузер не смог сохранить выбранную область. Обновите браузер или выберите другое фото.');
        return;
      }

      const avatar = document.createElement('img');
      avatar.src = canvas.toDataURL('image/jpeg', 0.9);
      avatar.alt = 'Предпросмотр аватара';
      preview.replaceChildren(avatar);
      uploadHint.textContent = 'Загружаем и сохраняем аватар…';
      uploadHint.style.color = '';
      status.textContent = 'Загружаем аватар…';

      if (!profileForm) {
        showError('Не удалось найти форму профиля. Обновите страницу и попробуйте снова.');
        return;
      }
      if (!profileForm.checkValidity()) {
        closeCropper(true);
        uploadHint.textContent = 'Фото подготовлено. Исправьте отмеченное поле и нажмите «Сохранить профиль».';
        applyButton.disabled = false;
        profileForm.reportValidity();
        return;
      }
      // После подтверждения кадра сразу отправляем форму: отдельный второй клик
      // «Сохранить профиль» больше не нужен и фото не остаётся только в превью.
      window.setTimeout(() => profileForm.requestSubmit(), 80);
    }, 'image/jpeg', 0.9);
  });

  cancelButton?.addEventListener('click', () => closeCropper());
  closeButton?.addEventListener('click', () => closeCropper());
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    closeCropper();
  });
})();
