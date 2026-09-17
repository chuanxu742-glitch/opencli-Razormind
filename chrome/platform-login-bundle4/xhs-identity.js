const ENDPOINTS = Object.freeze({web:'https://edith.xiaohongshu.com/api/sns/web/v2/user/me', creator:'https://creator.xiaohongshu.com/api/galaxy/creator/home/personal_info'});
const boundedText = (value, limit) => typeof value === 'string' && value.trim().length > 0 && value.length <= limit;
const unknown = () => ({evidence_kind:'unknown', external_identity:null, browser_session_state:'unknown'});

// WWW can establish a stable identity. Creator can only establish a separate
// authenticated session signal; its display name/number never leave this module.
export function createXhsIdentityProbe({fetcher = (...args) => fetch(...args), now = Date.now, verify}) {
  const pending = new Map();
  return async (message, sender) => {
    await verify(message, sender);
    const key = `${sender.tab.id}:${sender.documentId}`;
    let entry = pending.get(key);
    if (!entry || now() - entry.started >= 60000) {
      for (const [oldKey,oldEntry] of pending) {
        if (oldEntry.settled && now() - oldEntry.started >= 60000) pending.delete(oldKey);
      }
      if (!pending.has(key) && pending.size >= 64) return unknown();
      entry = {started: now(), viewGeneration:message.target?.viewGeneration};
      entry.promise = (async () => {
        const result = unknown();
        const readEndpoint = async source => {
          try {
            const response = await fetcher(ENDPOINTS[source], {credentials:'include', redirect:'error', cache:'no-store', signal:AbortSignal.timeout(5000)});
            if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) return;
            const reader = response.body.getReader();
            const chunks = []; let size = 0;
            while (true) {
              const {done,value} = await reader.read();
              if (done) break;
              size += value.byteLength;
              if (size > 65536) { await reader.cancel(); return; }
              chunks.push(value);
            }
            const buffer = new Uint8Array(size); let offset = 0;
            for (const chunk of chunks) { buffer.set(chunk,offset); offset += chunk.byteLength; }
            const body = JSON.parse(new TextDecoder().decode(buffer));
            if (body?.code !== 0 || body.success === false) return;
            if (source === 'web') {
              if (body.data?.guest === false && typeof body.data?.user_id === 'string' && /^[a-f0-9]{24}$/i.test(body.data.user_id)) {
                result.external_identity = {provider:'xiaohongshu',subject:body.data.user_id,label:body.data.user_id};
                result.evidence_kind = 'valid';
              } else if (body.data?.guest === true) result.evidence_kind = 'invalid';
            } else if (response.status === 200 && boundedText(body.data?.name,128) &&
                (boundedText(body.data?.red_num,64) || (Number.isSafeInteger(body.data?.red_num) && body.data.red_num > 0))) {
              result.browser_session_state = 'session_authenticated';
            }
          } catch { /* Request failures never establish logout or expose raw data. */ }
        };
        await readEndpoint('web');
        await verify(message,sender);
        // A different creator session cannot override explicit WWW logout.
        if (result.evidence_kind === 'unknown') await readEndpoint('creator');
        return {...result, observed_at:new Date(entry.started).toISOString()};
      })();
      entry.promise.then(() => { entry.settled = true; }, () => { entry.settled = true; });
      pending.set(key,entry);
    }
    const result = await entry.promise;
    await verify(message, sender);
    if (entry.viewGeneration !== message.target?.viewGeneration) return unknown();
    return result;
  };
}
