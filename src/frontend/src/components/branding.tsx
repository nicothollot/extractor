/* Houlihan Lokey brand assets as React components. The official logo SVGs are
   imported verbatim (never recolored, stretched or recreated); only the canvas
   crop differs between the full signature+mark and the globe-only mark. */
import logoColor from "../assets/hl-logo.svg";
import logoWhite from "../assets/hl-logo-white.svg";
import markColor from "../assets/hl-mark.svg";
import markWhite from "../assets/hl-mark-white.svg";

type Tone = "color" | "white";

/** The full Houlihan Lokey signature + mark (globe + wordmark). */
export function HLLogo({ tone = "color", className, height }: { tone?: Tone; className?: string; height?: number }) {
  return (
    <img
      src={tone === "white" ? logoWhite : logoColor}
      alt="Houlihan Lokey"
      className={className}
      style={height ? { height } : undefined}
      draggable={false}
    />
  );
}

/** The globe mark on its own (square). */
export function HLMark({ tone = "color", size, className }: { tone?: Tone; size?: number; className?: string }) {
  return (
    <img
      src={tone === "white" ? markWhite : markColor}
      alt=""
      aria-hidden
      className={className}
      style={size ? { width: size, height: size } : undefined}
      draggable={false}
    />
  );
}

/**
 * Branded loading indicator: the HL globe holds still (gently pulsing) while a
 * Sapphire-blue arc orbits it. The logo is never rotated or distorted — only
 * the surrounding ring animates, so it stays brand-compliant.
 */
export function HLSpinner({ size = 44, tone = "color" }: { size?: number; tone?: Tone }) {
  const arc = tone === "white" ? "rgba(255,255,255,0.92)" : "var(--hl-blue)";
  const track = tone === "white" ? "rgba(255,255,255,0.18)" : "var(--hl-gray-200)";
  return (
    <span
      role="status"
      aria-label="Loading"
      className="relative inline-flex items-center justify-center"
      style={{ width: size, height: size }}
    >
      <svg
        className="hl-spin-ring absolute inset-0"
        width={size}
        height={size}
        viewBox="0 0 50 50"
        fill="none"
        aria-hidden
      >
        <circle cx="25" cy="25" r="22" stroke={track} strokeWidth="2.5" />
        <circle
          cx="25"
          cy="25"
          r="22"
          stroke={arc}
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeDasharray="36 200"
        />
      </svg>
      <HLMark tone={tone} size={Math.round(size * 0.5)} className="hl-mark-pulse" />
    </span>
  );
}

/** Centered loading block with the branded spinner and an optional label. */
export function HLLoading({ label = "Loading…", size = 48, tone = "color" }: { label?: string; size?: number; tone?: Tone }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-10 text-ink-500">
      <HLSpinner size={size} tone={tone} />
      {label && <p className="text-[12.5px]">{label}</p>}
    </div>
  );
}
