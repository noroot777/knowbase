function xIntakeStatus(source) {
  const state = source.x_intake;
  if (!state) return '';
  const parts = [state.discovery === 'complete' ? '最新发现已完成' : state.discovery === 'partial' ? '最新发现未读完' : '时间线尚未轮到'];
  if (state.gap) parts.push(`历史缺口：${state.gap}`);
  if (state.pending) parts.push(`正文待补 ${state.pending} 条`);
  if (state.expired) parts.push(`超出五天自动窗口 ${state.expired} 条（未计完成）`);
  if (state.outside) parts.push(state.outside);
  if (state.error) parts.push(state.error);
  return `<p class="source-row-meta x-intake-status">${esc(parts.join(' · '))}</p>`;
}

function xIntakeTiming(run) {
  if (!run) return '';
  const labels = {prepare: '准备', startup: '启动', discovery: '发现', history: '历史', body: '正文', cleanup: '关闭', finalize: '落盘确认', delivery: '成品投递'};
  const stages = Object.entries(run.phases || {}).map(([name, value]) => {
    const wait = value.cooldown_seconds ? `（含冷却 ${value.cooldown_seconds.toFixed(1)}s）` : '';
    return `${labels[name] || name} ${(value.wall_seconds || 0).toFixed(1)}s${wait}`;
  });
  return ` · X ${run.elapsed_seconds}s：${stages.join(' / ')} · 待补 ${run.counts?.pending || 0} 条 · 有界重扫，非原生游标续页`;
}
