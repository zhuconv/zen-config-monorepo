/* Copying and saving what an agent wrote: a whole answer as Markdown, or one
 * code block as a file of its language. A saved file goes where Firefox puts
 * downloads, or where the user picks if Firefox is set to ask, and shows in
 * the downloads panel like any other download.
 *
 * The file is written here rather than downloaded from a data: URL: that
 * takes 5 ms instead of about 100, and a script (.sh, .py) is not held up by
 * the download reputation check. */

import { marked } from "./vendor/marked.esm.js";

const lazy = {};
ChromeUtils.defineESModuleGetters(lazy, {
  DownloadLastDir: "resource://gre/modules/DownloadLastDir.sys.mjs",
  DownloadPaths: "resource://gre/modules/DownloadPaths.sys.mjs",
  Downloads: "resource://gre/modules/Downloads.sys.mjs",
  FileUtils: "resource://gre/modules/FileUtils.sys.mjs",
  PrivateBrowsingUtils: "resource://gre/modules/PrivateBrowsingUtils.sys.mjs",
});

// A code block's language -> the extension it is saved with (else .txt).
const EXTENSIONS = {
  markdown: "md", md: "md",
  python: "py", py: "py", python3: "py",
  javascript: "js", js: "js", typescript: "ts", ts: "ts", jsx: "jsx", tsx: "tsx",
  json: "json", yaml: "yaml", yml: "yaml", toml: "toml", xml: "xml", html: "html", css: "css",
  latex: "tex", tex: "tex", bibtex: "bib", bib: "bib",
  bash: "sh", sh: "sh", shell: "sh", zsh: "sh",
  c: "c", cpp: "cpp", "c++": "cpp", java: "java", go: "go", rust: "rs", rs: "rs",
  ruby: "rb", swift: "swift", kotlin: "kt", r: "r", julia: "jl", matlab: "m",
  sql: "sql", csv: "csv", diff: "diff",
};

export function extensionFor(language) {
  return EXTENSIONS[(language || "").toLowerCase()] ?? "txt";
}

/** Put text on the clipboard. */
export function copyText(window, text) {
  Cc["@mozilla.org/widget/clipboardhelper;1"]
    .getService(Ci.nsIClipboardHelper)
    .copyString(text, window.browsingContext?.currentWindowContext ?? null);
}

/**
 * A file name: Markdown is named after its first heading; anything else, or
 * Markdown without one, after `fallback` (the page's title).
 */
export function fileName(text, extension, fallback) {
  let base = "";
  if (extension === "md") {
    base = marked.lexer(text).find(token => token.type === "heading")?.text ?? "";
    base = base.replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/[*_`~]/g, "");
  }
  base = base.trim() || (fallback ?? "").replace(/\.(pdf|html?)$/i, "").trim() || "Answer";
  base = [...base.replace(/\s*:\s*/g, " - ").replace(/[/\\]/g, "-")].slice(0, 80).join("");
  return lazy.DownloadPaths.sanitize(`${base.replace(/[\s.]+$/, "")}.${extension}`);
}

/**
 * Save text as a file named `name`. `source` is the page the answer was
 * about, shown in the downloads panel as where the file came from. Returns
 * the file's path, or null when the user cancels the file dialog.
 */
export async function saveText(window, { name, text, source }) {
  const top = window.browsingContext.topChromeWindow;
  const isPrivate = lazy.PrivateBrowsingUtils.isWindowPrivate(top);
  const file = Services.prefs.getBoolPref("browser.download.useDownloadDir", true)
    ? lazy.DownloadPaths.createNiceUniqueFile(
        new lazy.FileUtils.File(PathUtils.join(await lazy.Downloads.getPreferredDownloadsDirectory(), name))
      )
    : await askWhere(top, name);
  if (!file) {
    return null;
  }
  const bytes = await IOUtils.writeUTF8(file.path, text);
  try {
    const download = await lazy.Downloads.createDownload({
      source: {
        url: /^https?:/.test(source ?? "") ? source : `data:text/plain;charset=utf-8,${encodeURIComponent(text)}`,
        isPrivate,
      },
      target: file.path,
      succeeded: true,
      totalBytes: bytes,
      contentType: mimeTypeOf(name),
      startTime: new Date(),
    });
    await download.refresh();
    const list = await lazy.Downloads.getList(isPrivate ? lazy.Downloads.PRIVATE : lazy.Downloads.PUBLIC);
    await list.add(download);
  } catch (e) {
    console.error("[agent-sidebar] saved, but could not list the file in the downloads panel", e);
  }
  return file.path;
}

/** The Save As dialog, opened where the last one was. */
async function askWhere(top, name) {
  const lastDir = new lazy.DownloadLastDir(top);
  let dir = await lastDir.getFileAsync(null);
  if (!dir || !(await IOUtils.exists(dir.path))) {
    dir = new lazy.FileUtils.File(await lazy.Downloads.getPreferredDownloadsDirectory());
  }
  let title = "Save As";
  try {
    title = Services.strings
      .createBundle("chrome://global/locale/contentAreaCommands.properties")
      .GetStringFromName("SaveLinkTitle");
  } catch {}
  const picker = Cc["@mozilla.org/filepicker;1"].createInstance(Ci.nsIFilePicker);
  picker.init(top.browsingContext, title, Ci.nsIFilePicker.modeSave);
  picker.displayDirectory = dir;
  picker.defaultString = name;
  const extension = name.slice(name.lastIndexOf(".") + 1);
  picker.defaultExtension = extension;
  picker.appendFilter(`*.${extension}`, `*.${extension}`);
  picker.appendFilters(Ci.nsIFilePicker.filterAll);
  const result = await new Promise(resolve => picker.open(resolve));
  if (result === Ci.nsIFilePicker.returnCancel || !picker.file) {
    return null;
  }
  lastDir.setFile(null, picker.file.parent);
  return picker.file;
}

function mimeTypeOf(name) {
  try {
    return Cc["@mozilla.org/mime;1"]
      .getService(Ci.nsIMIMEService)
      .getTypeFromExtension(name.slice(name.lastIndexOf(".") + 1));
  } catch {
    return "text/plain";
  }
}
