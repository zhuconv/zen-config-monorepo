/* Agent Sidebar, content side. agent-sidebar.uc.js asks a tab for:
 *
 *   GetSelection  the text the user selected in this frame (or in a focused
 *                 text field).
 *   GetPageText   the page's readable text. In Firefox's PDF viewer this is
 *                 the PDF's text, page by page.
 *
 * Nothing here runs until the parent asks.
 */

import { setTimeout } from "resource://gre/modules/Timer.sys.mjs";

const READABILITY = "moz-src:///toolkit/components/reader/readability/Readability.js";
// PDF pages asked of pdf.js at once: one round trip per batch, not per page.
const PDF_BATCH = 8;

let readability = null; // Readability's scope, loaded once per content process

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

  #htmlText(maxChars) {
    let text = "";
    try {
      if (!readability) {
        readability = {};
        Services.scriptloader.loadSubScript(READABILITY, readability);
      }
      const article = new readability.Readability(this.document.cloneNode(true), {
        charThreshold: 200,
      }).parse();
      text = article?.textContent?.replace(/\n{3,}/g, "\n\n").trim() ?? "";
    } catch (e) {
      console.warn("[agent-sidebar] Readability failed", e);
    }
    // Readability keeps only the main article; short results usually mean the
    // page is not an article, so take everything visible instead.
    if (text.length < 500) {
      text = this.document.body?.innerText?.replace(/\n{3,}/g, "\n\n").trim() ?? "";
    }
    return {
      kind: "html",
      text: text.slice(0, maxChars),
      truncated: text.length > maxChars,
    };
  }
}
