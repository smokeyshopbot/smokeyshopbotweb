(() => {
  'use strict';
  const picker = document.querySelector('[data-message-emoji-picker]');
  if (!picker) return;
  const id = picker.querySelector('[data-message-emoji-id]');
  const fallback = picker.querySelector('[data-message-emoji-fallback]');
  const error = picker.querySelector('[data-message-emoji-error]');
  const atom = String.raw`\p{Extended_Pictographic}[\uFE0E\uFE0F]?[\u{1F3FB}-\u{1F3FF}]?`;
  const emoji = new RegExp(String.raw`^(?:\u{1F3F4}[\u{E0020}-\u{E007E}]{1,16}\u{E007F}|[\u{1F1E6}-\u{1F1FF}]{2}|[#*0-9]\uFE0F?\u20E3|` + atom + String.raw`(?:\u200D` + atom + ')*)$', 'u');
  const isEmoji = value => value.length <= 64 && emoji.test(value);
  const token = /\[\[emoji:([1-9][0-9]{0,19})\|([^\[\]\r\n|]+)\]\]/gi;
  let target = null, start = 0, end = 0;

  function check(value, field) {
    let icons = 0, invalid = false;
    const visible = value.replace(token, (all, emojiId, normal) => {
      icons += 1;
      if (!isEmoji(normal)) invalid = true;
      return normal;
    });
    if (invalid || /\[\[emoji:/i.test(visible)) {
      return 'Invalid premium emoji placeholder. Use Insert premium emoji to add it again.';
    }
    if (field.dataset.emojiIcons && icons > Number(field.dataset.emojiIcons)) {
      return 'Each answer button supports one premium emoji.';
    }
    const limit = Number(field.dataset.emojiLimit || 0);
    if (limit && visible.length > limit) return 'Use at most ' + limit + ' characters, counting the displayed emojis.';
    return '';
  }

  function close() {
    picker.close();
    if (target && target.isConnected) target.focus();
  }

  document.querySelectorAll('textarea[data-message-emojis]').forEach(field => {
    const controls = document.createElement('span');
    controls.className = 'message-emoji-controls';
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.insertMessageEmoji = '';
    button.textContent = 'Insert premium emoji';
    controls.appendChild(button);
    field.insertAdjacentElement('afterend', controls);
  });

  // Delegation also covers answer rows cloned with Add another option.
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-insert-message-emoji]');
    if (!button) return;
    event.preventDefault();
    target = button.closest('label').querySelector('textarea[data-message-emojis]');
    start = target.selectionStart;
    end = target.selectionEnd;
    error.hidden = true;
    picker.showModal();
    id.focus();
  });

  picker.querySelector('[data-message-emoji-cancel]').addEventListener('click', close);
  picker.querySelector('[data-message-emoji-insert]').addEventListener('click', () => {
    const identifier = id.value.trim();
    let problem = /^[1-9][0-9]{0,19}$/.test(identifier) ? '' : 'Enter a numeric Telegram emoji ID, up to 20 digits.';
    const normal = fallback.value.trim();
    if (!problem && !isEmoji(normal)) problem = 'Paste exactly one normal fallback emoji.';
    const placeholder = '[[emoji:' + identifier + '|' + normal + ']]';
    if (!problem) problem = check(target.value.slice(0, start) + placeholder + target.value.slice(end), target);
    if (problem) {
      error.textContent = problem;
      error.hidden = false;
      return;
    }
    target.setRangeText(placeholder, start, end, 'end');
    target.dispatchEvent(new Event('input', {bubbles: true}));
    close();
  });

  document.addEventListener('input', event => {
    if (event.target.matches('textarea[data-message-emojis]')) {
      event.target.setCustomValidity(check(event.target.value, event.target));
    }
  });
  document.addEventListener('submit', event => {
    const fields = [...event.target.querySelectorAll('textarea[data-message-emojis]')];
    fields.forEach(field => field.setCustomValidity(check(field.value, field)));
    if (fields.some(field => !field.validity.valid)) {
      event.preventDefault();
      event.target.reportValidity();
    }
  }, true);
})();
