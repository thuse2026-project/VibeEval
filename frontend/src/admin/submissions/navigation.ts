import { statusLabels, type DataSource, type OverviewQuery } from './types';

export function readOverviewQuery(search: string): OverviewQuery {
  const params = new URLSearchParams(search);
  const status = params.get('status') ?? 'all';
  const page = Number(params.get('page') ?? 1);
  const exam = params.get('exam_id') ?? '';
  return {
    source: params.get('source') === 'demo' ? 'demo' : 'live',
    exam: exam.length <= 120 ? exam : '',
    status: Object.hasOwn(statusLabels, status) ? status as OverviewQuery['status'] : 'all',
    page: Number.isSafeInteger(page) && page > 0 ? page : 1,
  };
}

export function overviewUrl(query: OverviewQuery): string {
  const params = new URLSearchParams({ source: query.source, status: query.status, page: String(query.page) });
  if (query.exam) params.set('exam_id', query.exam);
  return `/admin?${params}`;
}

export function submissionUrl(id: string, query: OverviewQuery): string {
  const params = new URLSearchParams({ source: query.source, return_to: overviewUrl(query) });
  return `/admin/submissions/${encodeURIComponent(id)}?${params}`;
}

export function returnToOverview(search: string, source: DataSource): string {
  const fallback = overviewUrl({ source, exam: '', status: 'all', page: 1 });
  const value = new URLSearchParams(search).get('return_to');
  if (!value) return fallback;
  try {
    const parsed = new URL(value, window.location.origin);
    if (parsed.origin !== window.location.origin || parsed.pathname !== '/admin') return fallback;
    const query = readOverviewQuery(parsed.search);
    return query.source === source ? overviewUrl(query) : fallback;
  } catch { return fallback; }
}
