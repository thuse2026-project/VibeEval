import DOMPurify from 'dompurify'
import { Marked } from 'marked'

const markdown = new Marked({
  gfm: true,
  renderer: {
    html: token => token.text.replace(/[&<>"']/g, character => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    })[character]!),
  },
})

export function renderMarkdown(source: string, localOnly = false) {
  return { __html: DOMPurify.sanitize(markdown.parse(source) as string, localOnly ? { FORBID_TAGS: ['img', 'video', 'audio', 'source'] } : {}) }
}

