/* Agent Sidebar, content side. agent-sidebar.uc.js asks a tab for:
 *
 *   GetSelection  the text the user selected in this frame (or in a focused
 *                 text field).
 *   GetPageText   the page's text as the user sees it. In Firefox's PDF viewer
 *                 this is the PDF's text, page by page.
 *
 * Nothing here runs until the parent asks.
 */

import { setTimeout } from "resource://gre/modules/Timer.sys.mjs";

// PDF pages asked of pdf.js at once: one round trip per batch, not per page.
const PDF_BATCH = 8;
// The parts of a page that belong to the site rather than to the page: its
// navigation, and the header and footer of the whole page. The header and
// footer of an article or section stay (#isSitePart).
const SITE_PARTS = "nav, header, footer, [role=navigation], [role=banner], [role=contentinfo]";

export class AgentSidebarChild extends JSWindowActorChild {
  receiveMessage({ name, data }) {
    switch (name) {
      case "GetSelection":
        return this.#selection();
      case "GetPageText":
        return this.#pageText(data?.maxChars ?? 120000);
    }
    return null;
  }

  #selection() {
    const win = this.contentWindow;
    if (!win) {
      return { selection: "" };
    }
    const text = win.getSelection()?.toString().trim();
    if (text) {
      return { selection: text };
    }
    // Text selected inside an <input> or <textarea> is not part of the
    // document selection.
    const field = this.document.activeElement;
    try {
      const { selectionStart: start, selectionEnd: end, value } = field ?? {};
      if (typeof value === "string" && end > start) {
        return { selection: value.slice(start, end).trim() };
      }
    } catch {}
    return { selection: "" };
  }

  async #pageText(maxChars) {
    const win = this.contentWindow;
    if (!win || !this.document) {
      return null;
    }
    const viewer = Cu.waiveXrays(win).PDFViewerApplication;
    return viewer ? this.#pdfText(viewer, maxChars) : this.#htmlText(maxChars);
  }

  async #until(fn, timeout) {
    const end = Date.now() + timeout;
    for (;;) {
      if (!this.contentWindow) {
        return null;
      }
      let value;
      try {
        value = fn();
      } catch {}
      if (value || Date.now() > end) {
        return value || null;
      }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
  }

  async #pdfText(viewer, maxChars) {
    await viewer.initializedPromise;
    const pdf = await this.#until(() => viewer.pdfDocument, 60000);
    if (!pdf) {
      return null;
    }
    const pages = pdf.numPages;
    let text = "";
    let pagesRead = 0;
    for (let first = 1; first <= pages && text.length < maxChars; first += PDF_BATCH) {
      const numbers = [];
      for (let n = first; n < first + PDF_BATCH && n <= pages; n++) {
        numbers.push(n);
      }
      const batch = await Promise.all(numbers.map(n => this.#pdfPageText(pdf, n)));
      for (const pageText of batch) {
        if (text.length >= maxChars) {
          break;
        }
        pagesRead++;
        text += `\n\n[page ${pagesRead}]\n${pageText}`;
      }
    }
    return {
      kind: "pdf",
      text: text.trim().slice(0, maxChars),
      pages,
      pagesRead,
      truncated: text.length > maxChars || pagesRead < pages,
    };
  }

  async #pdfPageText(pdf, number) {
    // Values that come back through the viewer's promises are wrapped
    // again, which hides pdf.js's own methods; waive each one.
    const page = Cu.waiveXrays(await pdf.getPage(number));
    const content = Cu.waiveXrays(await page.getTextContent());
    let text = "";
    for (const item of content.items) {
      if (typeof item.str === "string") {
        text += item.str + (item.hasEOL ? "\n" : "");
      }
    }
    return text.trim();
  }

  /**
   * The page as it is rendered: the text the user can see (innerText, so
   * nothing hidden), without the site's navigation, header and footer.
   * Reader mode's Readability would keep only what it takes for the article,
   * and drop comments, reviews and replies.
   */
  #htmlText(maxChars) {
    const body = this.document.body;
    if (!body) {
      return null;
    }
    const parts = [];
    this.#renderedText(body, parts);
    const text = parts
      .join("\n")
      .replace(/[ \t]+\n/g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
    return {
      kind: "html",
      text: text.slice(0, maxChars),
      truncated: text.length > maxChars,
    };
  }

  /** Whether an element belongs to the site rather than the page. A header or
   * footer inside an article or section is that article's own. */
  #isSitePart(el) {
    if (!el.matches(SITE_PARTS)) {
      return false;
    }
    if ((el.localName === "header" || el.localName === "footer") && !el.hasAttribute("role")) {
      return !el.parentElement?.closest("article, aside, main, nav, section");
    }
    return true;
  }

  /** The rendered text of `el` without the site parts in it: read whole when
   * it has none, and child by child when it does. */
  #renderedText(el, parts) {
    if (!el.querySelector(SITE_PARTS)) {
      const text = el.innerText?.trim();
      if (text) {
        parts.push(text);
      }
      return;
    }
    for (const child of el.childNodes) {
      if (child.nodeType === 3) {
        const text = child.textContent.trim();
        if (text) {
          parts.push(text);
        }
      } else if (child.nodeType === 1 && !this.#isSitePart(child) && child.checkVisibility()) {
        this.#renderedText(child, parts);
      }
    }
  }
}
