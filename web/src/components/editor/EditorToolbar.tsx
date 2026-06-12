import { useEditor } from "@tiptap/react";
import {
  AlignCenter, AlignLeft, AlignRight, Baseline, Bold, Eraser,
  Heading1, Heading2, Highlighter, ImagePlus, Italic, LinkIcon,
  List, ListOrdered, Quote, Redo2, Save, Strikethrough,
  Underline as UnderlineIcon, Undo2,
} from "lucide-react";

export function EditorToolbar({
  editor,
  onImageUpload,
  imageSelected,
  onSaveImage,
}: {
  editor: ReturnType<typeof useEditor>;
  onImageUpload: (files: File[]) => Promise<void>;
  imageSelected: boolean;
  onSaveImage: () => void;
}) {
  if (!editor) return null;

  const currentFontSize = (editor.getAttributes("textStyle").fontSize as string | undefined) ?? "15px";

  function setFontSize(size: string) {
    editor.chain().focus().setMark("textStyle", { fontSize: size === "15px" ? null : size }).run();
  }

  return (
    <div className="toolbar">
      <select className="toolbarSelect" title="字号" value={currentFontSize} onChange={(e) => setFontSize(e.target.value)}>
        {["12px", "14px", "15px", "16px", "18px", "20px", "24px"].map((s) => (
          <option key={s} value={s}>{s.replace("px", "")}px</option>
        ))}
      </select>
      <span className="toolbarSep" />

      <label className="toolbarColorBtn" title="字体颜色">
        <Baseline size={14} />
        <span className="toolbarColorBar" style={{ background: (editor.getAttributes("textStyle").color as string | undefined) ?? "#1a1a1a" }} />
        <input type="color" value={(editor.getAttributes("textStyle").color as string | undefined) ?? "#1a1a1a"} onChange={(e) => editor.chain().focus().setColor(e.target.value).run()} />
      </label>

      <label className="toolbarColorBtn" title="高亮背景">
        <Highlighter size={14} />
        <span className="toolbarColorBar" style={{ background: (editor.getAttributes("highlight").color as string | undefined) ?? "#ffd166" }} />
        <input type="color" value={(editor.getAttributes("highlight").color as string | undefined) ?? "#ffd166"} onChange={(e) => editor.chain().focus().setHighlight({ color: e.target.value }).run()} />
      </label>

      <span className="toolbarSep" />

      <button className={editor.isActive("bold") ? "active" : ""} title="加粗" type="button" onClick={() => editor.chain().focus().toggleBold().run()}><Bold size={16} /></button>
      <button className={editor.isActive("italic") ? "active" : ""} title="斜体" type="button" onClick={() => editor.chain().focus().toggleItalic().run()}><Italic size={16} /></button>
      <button className={editor.isActive("underline") ? "active" : ""} title="下划线" type="button" onClick={() => editor.chain().focus().toggleUnderline().run()}><UnderlineIcon size={16} /></button>
      <button className={editor.isActive("strike") ? "active" : ""} title="删除线" type="button" onClick={() => editor.chain().focus().toggleStrike().run()}><Strikethrough size={16} /></button>

      <span className="toolbarSep" />

      <button className={editor.isActive({ textAlign: "left" }) ? "active" : ""} title="左对齐" type="button" onClick={() => editor.chain().focus().setTextAlign("left").run()}><AlignLeft size={16} /></button>
      <button className={editor.isActive({ textAlign: "center" }) ? "active" : ""} title="居中" type="button" onClick={() => editor.chain().focus().setTextAlign("center").run()}><AlignCenter size={16} /></button>
      <button className={editor.isActive({ textAlign: "right" }) ? "active" : ""} title="右对齐" type="button" onClick={() => editor.chain().focus().setTextAlign("right").run()}><AlignRight size={16} /></button>

      <span className="toolbarSep" />

      <button title="撤销" type="button" disabled={!editor.can().undo()} onClick={() => editor.chain().focus().undo().run()}><Undo2 size={16} /></button>
      <button title="重做" type="button" disabled={!editor.can().redo()} onClick={() => editor.chain().focus().redo().run()}><Redo2 size={16} /></button>
      <button title="清除格式" type="button" onClick={() => editor.chain().focus().unsetAllMarks().clearNodes().run()}><Eraser size={16} /></button>

      <span className="toolbarSep" />

      <button className={editor.isActive("heading", { level: 1 }) ? "active" : ""} title="一级标题" type="button" onClick={() => editor.chain().focus().toggleHeading({ level: 1 }).run()}><Heading1 size={16} /></button>
      <button className={editor.isActive("heading", { level: 2 }) ? "active" : ""} title="二级标题" type="button" onClick={() => editor.chain().focus().toggleHeading({ level: 2 }).run()}><Heading2 size={16} /></button>
      <button className={editor.isActive("bulletList") ? "active" : ""} title="无序列表" type="button" onClick={() => editor.chain().focus().toggleBulletList().run()}><List size={16} /></button>
      <button className={editor.isActive("orderedList") ? "active" : ""} title="有序列表" type="button" onClick={() => editor.chain().focus().toggleOrderedList().run()}><ListOrdered size={16} /></button>
      <button className={editor.isActive("blockquote") ? "active" : ""} title="引用" type="button" onClick={() => editor.chain().focus().toggleBlockquote().run()}><Quote size={16} /></button>
      <button title="链接" type="button" onClick={() => { const url = window.prompt("链接地址"); if (url) editor.chain().focus().setLink({ href: url }).run(); }}><LinkIcon size={16} /></button>
      <label className="toolbarFile" title="插入图片">
        <ImagePlus size={16} />
        <input accept="image/*" type="file" multiple onChange={(event) => { const files = Array.from(event.target.files ?? []); if (files.length) void onImageUpload(files); event.currentTarget.value = ""; }} />
      </label>

      <span className="toolbarSep" />

      <button
        onClick={onSaveImage}
        disabled={!imageSelected}
        title={imageSelected ? "图片保存到图库" : "先选中正文中的图片"}
        type="button"
      >
        <Save size={16} />
      </button>
    </div>
  );
}
