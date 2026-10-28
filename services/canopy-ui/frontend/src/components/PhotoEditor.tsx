import { useEffect, useRef, useState } from "react";

/** Rotate/crop step before a photo upload. Output matches the "tile"
 * derivative's 3:4 portrait aspect (photo-processing.ts) so a manual crop
 * composes cleanly with the server's own attention-cropping — this only
 * fixes framing/orientation upstream of it, it doesn't replace it.
 *
 * Pan/zoom (not freeform corner-drag) — same interaction model as any
 * avatar cropper: fewer edge cases than resizable crop handles, and the
 * only two problems worth solving here are "wrong way up" and "wrong part
 * of frame." */

const VPW = 300;
const VPH = 400;
const OUT_W = 900;
const OUT_H = 1200;

interface Props {
  file: File;
  onCancel: () => void;
  onSave: (blob: Blob) => void;
}

export function PhotoEditor({ file, onCancel, onSave }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [img, setImg] = useState<HTMLImageElement | null>(null);
  const [rotation, setRotation] = useState(0); // 0 | 90 | 180 | 270
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; panX: number; panY: number } | null>(null);

  useEffect(() => {
    const url = URL.createObjectURL(file);
    const el = new Image();
    el.onload = () => setImg(el);
    el.src = url;
    return () => URL.revokeObjectURL(url);
  }, [file]);

  function dims(vpw: number, vph: number) {
    if (!img) return { effW: 1, effH: 1, baseScale: 1 };
    const rotated90 = rotation % 180 !== 0;
    const effW = rotated90 ? img.height : img.width;
    const effH = rotated90 ? img.width : img.height;
    const baseScale = Math.max(vpw / effW, vph / effH);
    return { effW, effH, baseScale };
  }

  function clampPan(p: { x: number; y: number }, vpw: number, vph: number) {
    const { effW, effH, baseScale } = dims(vpw, vph);
    const scale = baseScale * zoom;
    const maxX = Math.max(0, (effW * scale - vpw) / 2);
    const maxY = Math.max(0, (effH * scale - vph) / 2);
    return {
      x: Math.min(maxX, Math.max(-maxX, p.x)),
      y: Math.min(maxY, Math.max(-maxY, p.y)),
    };
  }

  function draw() {
    const canvas = canvasRef.current;
    if (!canvas || !img) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const { baseScale } = dims(VPW, VPH);
    const scale = baseScale * zoom;
    ctx.clearRect(0, 0, VPW, VPH);
    ctx.save();
    ctx.translate(VPW / 2 + pan.x, VPH / 2 + pan.y);
    ctx.rotate((rotation * Math.PI) / 180);
    ctx.scale(scale, scale);
    ctx.drawImage(img, -img.width / 2, -img.height / 2);
    ctx.restore();
  }

  useEffect(draw, [img, rotation, zoom, pan]);

  function rotateBy(delta: number) {
    setRotation((r) => (((r + delta) % 360) + 360) % 360);
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }

  function onPointerDown(e: React.PointerEvent<HTMLCanvasElement>) {
    (e.target as HTMLCanvasElement).setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, panX: pan.x, panY: pan.y };
  }
  function onPointerMove(e: React.PointerEvent<HTMLCanvasElement>) {
    if (!drag.current) return;
    const next = {
      x: drag.current.panX + (e.clientX - drag.current.x),
      y: drag.current.panY + (e.clientY - drag.current.y),
    };
    setPan(clampPan(next, VPW, VPH));
  }
  function onPointerUp() {
    drag.current = null;
  }

  function save() {
    if (!img) return;
    const out = document.createElement("canvas");
    out.width = OUT_W;
    out.height = OUT_H;
    const ctx = out.getContext("2d");
    if (!ctx) return;
    const ratio = OUT_W / VPW;
    const { baseScale } = dims(OUT_W, OUT_H);
    const scale = baseScale * zoom;
    ctx.save();
    ctx.translate(OUT_W / 2 + pan.x * ratio, OUT_H / 2 + pan.y * ratio);
    ctx.rotate((rotation * Math.PI) / 180);
    ctx.scale(scale, scale);
    ctx.drawImage(img, -img.width / 2, -img.height / 2);
    ctx.restore();
    out.toBlob((blob) => blob && onSave(blob), "image/jpeg", 0.9);
  }

  return (
    <div className="photo-editor">
      <div className="photo-editor-canvas-wrap">
        <canvas
          ref={canvasRef}
          width={VPW}
          height={VPH}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerLeave={onPointerUp}
        />
      </div>
      <div className="photo-editor-controls">
        <div className="photo-editor-row">
          <button type="button" onClick={() => rotateBy(-90)}>
            ⟲ Rotate left
          </button>
          <button type="button" onClick={() => rotateBy(90)}>
            ⟳ Rotate right
          </button>
        </div>
        <label className="photo-editor-zoom">
          Zoom
          <input
            type="range"
            min={1}
            max={3}
            step={0.02}
            value={zoom}
            onChange={(e) => {
              const z = Number(e.target.value);
              setZoom(z);
              setPan((p) => clampPan(p, VPW, VPH));
            }}
          />
        </label>
        <div className="photo-editor-row">
          <button type="button" className="secondary" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="primary" onClick={save} disabled={!img}>
            Use photo
          </button>
        </div>
      </div>
    </div>
  );
}
