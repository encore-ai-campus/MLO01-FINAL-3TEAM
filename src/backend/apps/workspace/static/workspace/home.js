const deskHome = document.querySelector('#desk-home-view');
const deskCards = document.querySelector('#desk-cards');
const cardElements = new Map([...deskCards.children].map(card => [card.dataset.card, card]));
const storageKey = 'voc.work-home.' + deskHome.dataset.owner + '.v1';

function validCards(cards) {
  return Array.isArray(cards) && cards.length === cardElements.size &&
    new Set(cards.map(card => card?.id)).size === cardElements.size &&
    cards.every(card => cardElements.has(card?.id) && typeof card.visible === 'boolean');
}

function loadDeskCards() {
  const serverCards = JSON.parse(document.querySelector('#desk-saved-cards').textContent);
  if (validCards(serverCards)) return serverCards;
  try {
    const cards = JSON.parse(localStorage.getItem(storageKey));
    if (validCards(cards)) return cards;
  } catch {
    // Use the default order when old browser storage is unavailable.
  }
  return [...cardElements.keys()].map(id => ({id, visible: true}));
}

function openLegacySettings() {
  if (location.hash === '#02-02') {
    location.replace(document.querySelector('#desk-display-link').href);
  }
}

const savedCards = loadDeskCards();
for (const {id, visible} of savedCards) {
  const element = cardElements.get(id);
  element.hidden = !visible;
  deskCards.append(element);
}
document.querySelector('#desk-empty').hidden = savedCards.some(card => card.visible);

window.addEventListener('hashchange', openLegacySettings);
openLegacySettings();
