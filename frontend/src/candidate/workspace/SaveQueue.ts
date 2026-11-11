export type Draft = {
  content: string;
  version: number;
  readonly: boolean;
  dirty: boolean;
  error?: string;
};

// All files share one session lock, so serialize saves across files as well as within each file.
export class SaveQueue {
  drafts: Record<string, Draft> = {};
  private tail: Promise<void> = Promise.resolve();
  saving = false;
  constructor(
    private write: (path: string, version: number, content: string) => Promise<{ new_version: number }>,
    private changed: () => void,
  ) {}

  load(path: string, content: string, version: number, readonly: boolean) {
    this.drafts[path] = { content, version, readonly, dirty: false };
    this.changed();
  }

  edit(path: string, content: string) {
    const draft = this.drafts[path];
    if (!draft || draft.readonly) return;
    draft.content = content;
    draft.dirty = true;
    this.changed();
  }

  save(path: string): Promise<void> {
    const task = this.tail.then(async () => {
      const draft = this.drafts[path];
      if (!draft?.dirty || draft.readonly) return;
      if (draft.error) throw new Error(draft.error);
      const content = draft.content;
      this.saving = true;
      this.changed();
      try {
        const result = await this.write(path, draft.version, content);
        draft.version = result.new_version;
        draft.dirty = draft.content !== content;
      } catch (error) {
        draft.error = error instanceof Error ? error.message : '保存失败，请重新读取比较。';
        throw error;
      } finally {
        this.saving = false;
        this.changed();
      }
    });
    this.tail = task.catch(() => undefined);
    return task;
  }

  async flush() {
    await this.tail;
    for (const path of Object.keys(this.drafts)) await this.save(path);
    if (Object.values(this.drafts).some(draft => draft.dirty || draft.error)) {
      throw new Error('仍有文件未保存，请处理后再提交。');
    }
  }

  async settle() { await this.tail; }
}
