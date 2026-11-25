const deskHome = document.querySelector('#desk-home-view');
const deskSettings = document.querySelector('#desk-settings-view');
const deskCards = document.querySelector('#desk-cards');
const deskRows = document.querySelector('#desk-settings-rows');
const cardDefinitions = {
  tickets: ['내게 할당된 티켓', '오늘 배정된 티켓과 처리가 남은 티켓'],
  requests: ['받은 요청', '나에게 들어온 검토 요청'],
  performance: ['내 실적', '오늘과 전날의 처리 건수']
};
const defaultCards = () => Object.keys(cardDefinitions).map(id => ({ id, visible: true }));
const cardElements = new Map([...deskCards.children].map(card => [card.dataset.card, card]));
const storageKey = 'voc.work-home.' + deskHome.dataset.owner + '.v1';

function validCards(cards) {
  const ids = Object.keys(cardDefinitions);
  return Array.isArray(cards) && cards.length === ids.length &&
    new Set(cards.map(card => card?.id)).size === ids.length &&
    cards.every(card => ids.includes(card?.id) && typeof card.visible === 'boolean');
}

function loadDeskCards() {
  const serverCards = JSON.parse(document.querySelector('#desk-saved-cards').textContent);
  if (validCards(serverCards)) return serverCards;
  try {
    const cards = JSON.parse(localStorage.getItem(storageKey));
    if (validCards(cards)) return cards;
  } catch {
    // A local file preview can run with browser storage disabled.
  }
  return defaultCards();
}

let savedCards = loadDeskCards();
let editingCards = defaultCards();
let draggedCard = null;

function renderDeskHome() {
  for (const { id, visible } of savedCards) {
    const element = cardElements.get(id);
    element.hidden = !visible;
    deskCards.append(element);
  }
  document.querySelector('#desk-empty').hidden = savedCards.some(card => card.visible);
}

function deskIcon(id) {
  return '<svg aria-hidden="true"><use href="#desk-' + id + '"/></svg>';
}

function renderDeskSettings() {
  deskRows.innerHTML = editingCards.map(({ id, visible }, index) => {
    const [title, description] = cardDefinitions[id];
    const disabledUp = index === 0 ? ' disabled' : '';
    const disabledDown = index === editingCards.length - 1 ? ' disabled' : '';
    return '<div class="desk-setting-row" data-id="' + id + '">' +
      '<div class="desk-setting-title"><span class="desk-grip" draggable="true">' + deskIcon('grip') + '</span>' +
      '<span><strong>' + title + '</strong><small>' + description + '</small></span></div>' +
      '<button type="button" class="desk-switch" role="switch" aria-label="' + title + ' 표시" aria-checked="' + visible + '"></button>' +
      '<div class="desk-arrows"><button type="button" class="desk-arrow" data-direction="up" aria-label="' + title + ' 위로 이동"' + disabledUp + '>' + deskIcon('up') + '</button>' +
      '<button type="button" class="desk-arrow" data-direction="down" aria-label="' + title + ' 아래로 이동"' + disabledDown + '>' + deskIcon('down') + '</button></div></div>';
  }).join('');
}

function moveDeskCard(id, destination) {
  const index = editingCards.findIndex(card => card.id === id);
  if (index < 0 || destination < 0 || destination >= editingCards.length || index === destination) return;
  const [card] = editingCards.splice(index, 1);
  editingCards.splice(destination, 0, card);
  renderDeskSettings();
}

deskRows.addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button) return;
  const { id } = button.closest('[data-id]').dataset;
  const index = editingCards.findIndex(card => card.id === id);
  if (button.getAttribute('role') === 'switch') {
    editingCards[index].visible = !editingCards[index].visible;
    button.setAttribute('aria-checked', String(editingCards[index].visible));
    return;
  }
  const direction = button.dataset.direction;
  moveDeskCard(id, index + (direction === 'up' ? -1 : 1));
  const row = deskRows.querySelector('[data-id="' + id + '"]');
  (row.querySelector('[data-direction="' + direction + '"]:not(:disabled)') ||
    row.querySelector('.desk-arrow:not(:disabled)'))?.focus();
});

deskRows.addEventListener('dragstart', event => {
  const handle = event.target.closest('.desk-grip');
  if (!handle) return;
  draggedCard = handle.closest('[data-id]').dataset.id;
  event.dataTransfer.setData('text/plain', draggedCard);
  event.dataTransfer.effectAllowed = 'move';
});
deskRows.addEventListener('dragover', event => {
  if (draggedCard && event.target.closest('[data-id]')) event.preventDefault();
});
deskRows.addEventListener('drop', event => {
  const row = event.target.closest('[data-id]');
  if (!draggedCard || !row) return;
  event.preventDefault();
  moveDeskCard(draggedCard, editingCards.findIndex(card => card.id === row.dataset.id));
  draggedCard = null;
});
deskRows.addEventListener('dragend', () => { draggedCard = null; });

document.querySelector('#desk-reset').addEventListener('click', () => {
  editingCards = defaultCards();
  renderDeskSettings();
});
document.querySelector('#desk-card-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const saveButton = document.querySelector('#desk-save');
  const errorMessage = document.querySelector('#desk-card-error');
  const data = new FormData(form);
  data.set('cards', JSON.stringify(editingCards));
  saveButton.disabled = true;
  errorMessage.hidden = true;
  try {
    const response = await fetch(form.action, {method: 'POST', body: data});
    if (!response.ok || response.redirected) throw new Error('Save failed');
    savedCards = (await response.json()).cards;
    location.hash = '02-01';
  } catch {
    errorMessage.hidden = false;
  } finally {
    saveButton.disabled = false;
  }
});

function showScreenFromHash() {
  const settings = location.hash === '#02-02';
  deskHome.hidden = settings;
  deskSettings.hidden = !settings;
  document.title = (settings ? '화면 설정' : '업무 홈') + ' | 섭명뭐';
  if (settings) {
    editingCards = savedCards.map(card => ({ ...card }));
    renderDeskSettings();
  } else {
    renderDeskHome();
  }
  window.scrollTo(0, 0);
}
window.addEventListener('hashchange', showScreenFromHash);
showScreenFromHash();
