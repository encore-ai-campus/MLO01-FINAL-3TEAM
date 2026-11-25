const displayForm = document.querySelector('[data-display-settings]');
const cardRows = displayForm.querySelector('[data-card-rows]');
const cardInput = displayForm.elements.cards;
const defaultOrder = JSON.parse(document.querySelector('#display-default-order').textContent);
const rows = new Map([...cardRows.children].map(row => [row.dataset.card, row]));
let dragged = null;

function syncCards() {
  const orderedRows = [...cardRows.children];
  cardInput.value = JSON.stringify(orderedRows.map((row, index) => {
    row.querySelector('[data-move="up"]').disabled = index === 0;
    row.querySelector('[data-move="down"]').disabled = index === orderedRows.length - 1;
    return {id: row.dataset.card, visible: row.querySelector('[role="switch"]').getAttribute('aria-checked') === 'true'};
  }));
}

function applyCards(cards) {
  for (const {id, visible} of cards) {
    const row = rows.get(id);
    row.querySelector('[role="switch"]').setAttribute('aria-checked', String(visible));
    cardRows.append(row);
  }
  syncCards();
}

let initialCards = JSON.parse(cardInput.value);
if (displayForm.dataset.localCards) {
  try {
    const localCards = JSON.parse(localStorage.getItem(displayForm.dataset.localCards));
    if (Array.isArray(localCards) && localCards.length === rows.size &&
        localCards.every(card => rows.has(card?.id) && typeof card.visible === 'boolean') &&
        new Set(localCards.map(card => card.id)).size === rows.size) initialCards = localCards;
  } catch {
    // Keep the server defaults when old browser storage is unavailable.
  }
}
applyCards(initialCards);

cardRows.addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button) return;
  const row = button.closest('[data-card]');
  if (button.getAttribute('role') === 'switch') {
    button.setAttribute('aria-checked', String(button.getAttribute('aria-checked') !== 'true'));
  } else {
    const movingUp = button.dataset.move === 'up';
    const sibling = movingUp ? row.previousElementSibling : row.nextElementSibling;
    if (!sibling) return;
    if (movingUp) sibling.before(row);
    else sibling.after(row);
  }
  syncCards();
  (button.disabled ? row.querySelector('[data-move]:not(:disabled)') : button)?.focus();
});

cardRows.addEventListener('dragstart', event => {
  if (!event.target.closest('.desk-grip')) return;
  dragged = event.target.closest('[data-card]');
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', dragged.dataset.card);
});
cardRows.addEventListener('dragover', event => {
  if (dragged && event.target.closest('[data-card]')) event.preventDefault();
});
cardRows.addEventListener('drop', event => {
  const target = event.target.closest('[data-card]');
  if (!dragged || !target) return;
  event.preventDefault();
  if (dragged.compareDocumentPosition(target) & Node.DOCUMENT_POSITION_FOLLOWING) target.after(dragged);
  else if (dragged !== target) target.before(dragged);
  dragged = null;
  syncCards();
});
cardRows.addEventListener('dragend', () => { dragged = null; });
displayForm.querySelector('[data-reset-cards]').addEventListener('click', () => {
  applyCards(defaultOrder.map(id => ({id, visible: true})));
});
