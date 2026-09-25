async function runArticleJob(path, options, logElement) {
  logElement.hidden = false;
  logElement.replaceChildren();
  const response = await fetch(path, options);
  const started = await response.json();
  if (!response.ok) throw new Error(started.detail || 'Không bắt đầu được quá trình tải bài viết.');
  const jobId = started.job_id;
  let shown = 0;
  for (;;) {
    let snapshot;
    try {
      const poll = await fetch('/api/jobs/' + encodeURIComponent(jobId), {cache: 'no-store'});
      snapshot = await poll.json();
      if (!poll.ok) throw new Error(snapshot.detail || 'Không đọc được nhật ký tải bài viết.');
    } catch (error) {
      throw new Error('Mất kết nối khi theo dõi bài viết: ' + error.message);
    }
    for (const event of snapshot.events.slice(shown)) {
      const row = document.createElement('li');
      row.className = event.level === 'error' ? 'error' : '';
      row.textContent = new Date(event.time).toLocaleTimeString('vi-VN') + ' · ' + event.message;
      logElement.append(row);
    }
    shown = snapshot.events.length;
    if (snapshot.state === 'done') return snapshot.result;
    if (snapshot.state === 'error') throw new Error(snapshot.error || 'Không tải được bài viết.');
    await new Promise(resolve => setTimeout(resolve, 800));
  }
}
