/** Placeholder leaf mark for plants without a photo yet. */

export function Leafmark({ size = 72 }: { size?: number }) {
  return (
    <div className="leafmark">
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1"
      >
        <path d="M12 2C7 7 4 12 4 16a8 8 0 0 0 16 0c0-4-3-9-8-14z" />
        <path d="M12 22V8" />
      </svg>
    </div>
  );
}
