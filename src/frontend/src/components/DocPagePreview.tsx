import { useRef, useState } from "react";
import { candidatePreviewUrl } from "../lib/api";
import { Button } from "./ui";

/** Page preview for a candidate document (Confirm documents step), with page
 *  navigation, zoom in/out + reset (scroll/pan when zoomed), and a toggleable
 *  magnifying-glass lens that shows a crisp, higher-DPI crop following the
 *  cursor. Self-contained: owns its own page/zoom/lens state per document. */
export function DocPagePreview({ filePath, fileName }: { filePath: string; fileName?: string }) {
  const [page, setPage] = useState(1);
  const [maxPage, setMaxPage] = useState<number | null>(null);
  const [zoom, setZoom] = useState(1);
  const [lensOn, setLensOn] = useState(false);
  // Cursor position over the image (px, relative to the displayed image) and the
  // image's displayed size, for positioning the lens.
  const imgRef = useRef<HTMLImageElement | null>(null);
  const [lens, setLens] = useState<{ x: number; y: number; w: number; h: number } | null>(null);

  const LENS = 160; // lens diameter in px
  const LENS_ZOOM = 2.5; // how much the lens magnifies the displayed page

  const onMove = (e: React.MouseEvent) => {
    if (!lensOn || !imgRef.current) return;
    const r = imgRef.current.getBoundingClientRect();
    const x = e.clientX - r.left;
    const y = e.clientY - r.top;
    if (x < 0 || y < 0 || x > r.width || y > r.height) {
      setLens(null);
      return;
    }
    setLens({ x, y, w: r.width, h: r.height });
  };

  return (
    <div className="mt-2 border-t border-line pt-2">
      <div className="flex items-center justify-center gap-3 pb-2 text-[12px] text-ink-600 flex-wrap">
        <Button kind="ghost" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))} title="Previous page">
          ◀
        </Button>
        <span className="font-mono">page {page}</span>
        <Button
          kind="ghost"
          disabled={maxPage != null && page >= maxPage}
          onClick={() => setPage((p) => p + 1)}
          title="Next page"
        >
          ▶
        </Button>
        <span className="text-ink-300">|</span>
        <Button kind="ghost" disabled={zoom <= 0.5} onClick={() => setZoom((z) => Math.max(0.5, +(z - 0.25).toFixed(2)))} title="Zoom out">
          −
        </Button>
        <span className="font-mono w-12 text-center">{Math.round(zoom * 100)}%</span>
        <Button kind="ghost" disabled={zoom >= 4} onClick={() => setZoom((z) => Math.min(4, +(z + 0.25).toFixed(2)))} title="Zoom in">
          +
        </Button>
        <Button kind="ghost" disabled={zoom === 1} onClick={() => setZoom(1)} title="Reset zoom">
          reset
        </Button>
        <Button
          kind={lensOn ? "primary" : "ghost"}
          onClick={() => {
            setLensOn((v) => !v);
            setLens(null);
          }}
          title="Magnifying glass — hover over the page to magnify"
        >
          🔍 lens
        </Button>
      </div>

      <div className="max-h-[460px] overflow-auto flex justify-center">
        <div
          className="relative"
          onMouseMove={onMove}
          onMouseLeave={() => setLens(null)}
          style={{ cursor: lensOn ? "crosshair" : "default" }}
        >
          <img
            ref={imgRef}
            key={`${page}-${zoom}`}
            src={candidatePreviewUrl(filePath, page)}
            alt={fileName ? `Page ${page} of ${fileName}` : `Page ${page}`}
            style={{ width: `${zoom * 100}%`, maxWidth: zoom <= 1 ? "100%" : "none" }}
            className="h-auto border border-line rounded shadow-sm block"
            onError={() => {
              // Stepped past the last page (render 404): remember the max and
              // step back so the view stays on a real page.
              if (page > 1) {
                setMaxPage(page - 1);
                setPage((p) => Math.max(1, p - 1));
              }
            }}
          />
          {lensOn && lens && (
            <div
              className="pointer-events-none absolute rounded-full border-2 border-accent shadow-lg"
              style={{
                width: LENS,
                height: LENS,
                left: lens.x - LENS / 2,
                top: lens.y - LENS / 2,
                // Higher-DPI render of the same page for a crisp magnified view.
                backgroundImage: `url(${candidatePreviewUrl(filePath, page, 220)})`,
                backgroundRepeat: "no-repeat",
                backgroundSize: `${lens.w * LENS_ZOOM}px ${lens.h * LENS_ZOOM}px`,
                backgroundPositionX: `${-(lens.x * LENS_ZOOM - LENS / 2)}px`,
                backgroundPositionY: `${-(lens.y * LENS_ZOOM - LENS / 2)}px`,
                backgroundColor: "white",
              }}
            />
          )}
        </div>
      </div>
    </div>
  );
}
