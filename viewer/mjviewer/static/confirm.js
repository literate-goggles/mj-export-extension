// A small modal dialog that asks before an action: one that cannot be undone,
// or one that changes what other people see. Focus starts on Cancel; Esc and
// Cancel close it without acting. While the action runs the buttons are
// disabled and the dialog stays open, whatever tries to close it (a repeated
// Esc, its view going away); when the action fails, the failure is shown and
// the dialog stays open.

import {h, nextId} from './dom.js';
import {str} from './format.js';

let current = null;

/**
 * Show the dialog; action() runs when the confirm button is pressed, and the
 * dialog closes once it has succeeded. Resolves true when it succeeded, false
 * when the dialog was cancelled. A failure is never dropped: failure(err) is
 * shown in the dialog, and should the dialog be gone from the page by then,
 * the promise rejects with the error so that the caller can say so on the
 * page. returnFocus() names where focus goes back to on cancel when the
 * element that opened the dialog is gone. tone colours the confirm button:
 * 'danger' (the default) or 'primary'.
 */
export function confirmAction({title, body = null, confirmLabel, busyLabel = 'Working…',
  failure = (err) => `Could not finish: ${str(err && err.message) || 'unknown error'}`, action, returnFocus = null,
  tone = 'danger'}) {
  closeConfirm();
  const opener = document.activeElement;
  return new Promise((resolve, reject) => {
    const titleId = nextId('confirm-title');
    const bodyId = nextId('confirm-body');
    const error = h('p', {class: 'confirm__error', role: 'alert', hidden: true});
    const cancel = h('button', {type: 'button', class: 'btn', autofocus: true}, 'Cancel');
    const go = h('button', {type: 'button', class: ['btn', tone === 'primary' ? 'btn--primary' : 'btn--danger']},
      confirmLabel);
    const dialog = h('dialog', {class: 'confirm', 'aria-labelledby': titleId, 'aria-describedby': bodyId},
      h('h2', {class: 'confirm__title', id: titleId}, title),
      h('div', {class: 'confirm__body', id: bodyId}, body),
      error,
      h('div', {class: 'confirm__actions'}, cancel, go));
    let busy = false;
    let hidden = false;

    function show() {
      try {
        dialog.showModal();
      } catch {
        dialog.setAttribute('open', '');
      }
    }

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
        if (hidden || !dialog.isConnected || !dialog.open) {
          hide();
          reject(err);
          return;
        }
        error.textContent = failure(err);
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
      // A repeated Esc may close it without a cancelable event: while the action runs, it opens again at once.
      if (hidden) return;
      if (!busy) dismiss();
      else if (dialog.isConnected) show();
    });
    document.body.append(dialog);
    document.documentElement.classList.add('is-locked');
    show();
    cancel.focus();
    current = {dialog, close: dismiss};
  });
}

/** Close the dialog when its view goes away; while its action runs it stays open until that ends. */
export function closeConfirm() {
  if (current) current.close();
}

/** True while a confirmation dialog is open. */
export function confirmOpen() {
  return Boolean(current);
}
