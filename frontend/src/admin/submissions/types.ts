export type DataSource = 'live' | 'demo';

export const statusLabels = {
  invited: '尚未入场', admitted: '已入场，未开始', in_progress: '作答中', submitted: '已交卷',
  invitation_expired: '凭证已过期，未入场', recovery_expired: '恢复期限已过，未交卷',
};

export type OverviewQuery = {
  source: DataSource; exam: string; status: keyof typeof statusLabels | 'all'; page: number;
};
export type RecordRow = {
  candidate_id: string; candidate_name: string; exam_id: string; exam_title: string;
  status: keyof typeof statusLabels; session_id: string | null; admitted_at: number | null;
  started_at: number | null; submission_id: string | null; submitted_at: number | null;
};
export type OverviewData = {
  source: DataSource; items: RecordRow[]; total: number; page: number; page_size: number;
  summary: { total: number; not_submitted: number; submitted: number };
  exams: { id: string; title: string }[]; updated_at: number; submission_service_connected: boolean;
};
export type SubmissionData = {
  source: DataSource; submission_id: string; session_id: string; snapshot_id: string;
  candidate_id: string; candidate_name: string; exam_id: string; exam_title: string;
  admitted_at: number | null; started_at: number | null; submitted_at: number;
  files: { path: string; content: string }[]; public_test_output: string | null;
};

export const formatTime = (value: number) => new Date(value * 1000).toLocaleString('zh-CN', { hour12: false });
