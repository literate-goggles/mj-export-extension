// A small modal dialog that asks before an action that cannot be undone. Focus
// starts on Cancel; Esc and Cancel close it without acting. While the action
// runs the buttons are disabled; when it fails, its message is shown and the
// dialog stays open.

import {h, nextId} from './dom.js';
import {str} from './format.js';

let current = null;

/**
 * Show the dialog; action() runs when the danger button is pressed, and the
 * dialog closes once it has succeeded. Resolves true when it succeeded, false
 * when the dialog was cancelled. returnFocus() names where focus goes back to
 * on cancel when the element that opened the dialog is gone.
 */
export function confirmAction({title, body = null, confirmLabel, busyLabel = 'Working…', failure = 'Could not finish',
  action, returnFocus = null}) {
  closeConfirm();
  const opener = document.activeElement;
  return new Promise((resolve) => {
    const titleId = nextId('confirm-title');
    const bodyId = nextId('confirm-body');
    const error = h('p', {class: 'confirm__error', role: 'alert', hidden: true});
    const cancel = h('button', {type: 'button', class: 'btn', autofocus: true}, 'Cancel');
    const go = h('button', {type: 'button', class: 'btn btn--danger'}, confirmLabel);
    const dialog = h('dialog', {class: 'confirm', 'aria-labelledby': titleId, 'aria-describedby': bodyId},
      h('h2', {class: 'confirm__title', id: titleId}, title),
      h('div', {class: 'confirm__body', id: bodyId}, body),
      error,
      h('div', {class: 'confirm__actions'}, cancel, go));
    let busy = false;
    let hidden = false;

    function hide() {
      hidden = true;
      if (current && current.dialog === dialog) current = null;
      if (dialog.open) dialog.close();
      dialog.remove();
      document.documentElement.classList.remove('is-locked');
    }

    function dismiss() {
      if (busy) return;
      hide();
      const target = (returnFocus && returnFocus()) || opener;
      if (target && target.isConnected && typeof target.focus === 'function') target.focus();
      resolve(false);
    }

    async function run() {
      if (busy) return;
      busy = true;
      cancel.disabled = true;
      go.disabled = true;
      go.textContent = busyLabel;
      error.hidden = true;
      try {
        await action();
      } catch (err) {
        busy = false;
        cancel.disabled = false;
        go.disabled = false;
        go.textContent = confirmLabel;
        if (hidden) {
          resolve(false);
          return;
        }
        error.textContent = `${failure}: ${str(err && err.message) || 'unknown error'}`;
        error.hidden = false;
        cancel.focus();
        return;
      }
      hide();
      resolve(true);
    }

    cancel.addEventListener('click', dismiss);
    go.addEventListener('click', run);
    dialog.addEventListener('cancel', (event) => {  // Esc
      event.preventDefault();
      dismiss();
    });
    dialog.addEventListener('close', () => {
      // The browser may close it on a repeated Esc without a cancelable event; a running action still finishes.
      if (hidden) return;
      if (busy) hide();
      else dismiss();
    });
    document.body.append(dialog);
    document.documentElement.classList.add('is-locked');
    try {
      dialog.showModal();
    } catch {
      dialog.setAttribute('open', '');
    }
    cancel.focus();
    current = {dialog, close: () => (busy ? hide() : dismiss())};
  });
}

/** Close the dialog when its view goes away; an action already running still finishes. */
export function closeConfirm() {
  if (current) current.close();
}
