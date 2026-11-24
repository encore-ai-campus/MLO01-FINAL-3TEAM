const storeName = 'reviewReplies';
let connection;

function openStore() {
  if (!connection) {
    connection = new Promise((resolve, reject) => {
      const request = indexedDB.open('voc-inbox-preview', 1);
      request.onupgradeneeded = () => request.result.createObjectStore(storeName, { keyPath: 'requestId' });
      request.onsuccess = () => resolve(request.result);
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

export async function readReply(requestId, recipient) {
  const db = await openStore();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readwrite');
    const store = tx.objectStore(storeName);
    const request = store.get(requestId);
    let reply;
    request.onsuccess = () => {
      reply = request.result;
      if (reply?.recipient !== recipient) {
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
