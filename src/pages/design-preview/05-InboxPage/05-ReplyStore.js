const storeName = 'reviewReplies';
let connection;

export function openStore() {
  if (!connection) {
    connection = new Promise((resolve, reject) => {
      const request = indexedDB.open('voc-inbox-preview', 2);
      request.onupgradeneeded = () => {
        const database = request.result;
        if (!database.objectStoreNames.contains(storeName)) database.createObjectStore(storeName, { keyPath: 'requestId' });
        if (!database.objectStoreNames.contains('ticketEvents')) {
          database.createObjectStore('ticketEvents', { keyPath: 'eventId' }).createIndex('ticketId', 'ticketId');
        }
      };
      request.onblocked = () => reject(new Error('열려 있는 다른 시안 탭을 새로고침해 주세요.'));
      request.onsuccess = () => {
        const database = request.result;
        database.onversionchange = () => { database.close(); connection = null; };
        resolve(database);
      };
      request.onerror = () => reject(request.error);
    }).catch(error => {
      connection = null;
      throw error;
    });
  }
  return connection;
}

async function transact(mode, run) {
  const db = await openStore();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, mode);
    const request = run(tx.objectStore(storeName));
    tx.oncomplete = () => resolve(request.result);
    tx.onabort = () => reject(request.error ?? tx.error);
  });
}

export function loadReplies() {
  return transact('readonly', store => store.getAll());
}

export function saveReply(reply) {
  return transact('readwrite', store => store.add(reply));
}

export async function readReply(requestId, recipient, recipientId) {
  const db = await openStore();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readwrite');
    const store = tx.objectStore(storeName);
    const request = store.get(requestId);
    let reply;
    request.onsuccess = () => {
      reply = request.result;
      if (!reply || (reply.recipientId ? reply.recipientId !== recipientId : reply.recipient !== recipient)) {
        reply = undefined;
      } else if (!reply.read) {
        reply = { ...reply, read: true };
        store.put(reply);
      }
    };
    tx.oncomplete = () => resolve(reply);
    tx.onabort = () => reject(tx.error);
  });
}
