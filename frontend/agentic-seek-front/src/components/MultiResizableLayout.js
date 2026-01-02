import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import "./MultiResizableLayout.css";

const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));

export const MultiResizableLayout = ({
  children,
  widths,
  onWidthsChange,
  minPaneWidthPct = 15,
}) => {
  const containerRef = useRef(null);
  const [dragIdx, setDragIdx] = useState(null); // handle index (between idx and idx+1)
  const dragStart = useRef({ x: 0, widths: [] });

  const count = React.Children.count(children);

  const normWidths = useMemo(() => {
    const n = Math.max(1, count);
    const w = Array.isArray(widths) ? widths.slice(0, n) : [];
    while (w.length < n) w.push(100 / n);
    const sum = w.reduce((a, b) => a + (Number.isFinite(b) ? b : 0), 0) || 100;
    return w.map((x) => (Number.isFinite(x) ? (x * 100) / sum : 100 / n));
  }, [widths, count]);

  const handleMouseDown = useCallback(
    (idx, e) => {
      e.preventDefault();
      setDragIdx(idx);
      dragStart.current = { x: e.clientX, widths: normWidths.slice() };
    },
    [normWidths]
  );

  const handleMouseMove = useCallback(
    (e) => {
      if (dragIdx === null || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      if (!rect.width) return;
      const dxPct = ((e.clientX - dragStart.current.x) / rect.width) * 100;
      const startW = dragStart.current.widths.slice();

      const left = startW[dragIdx];
      const right = startW[dragIdx + 1];
      const total = left + right;

      const newLeft = clamp(left + dxPct, minPaneWidthPct, total - minPaneWidthPct);
      const newRight = total - newLeft;

      const next = startW.slice();
      next[dragIdx] = newLeft;
      next[dragIdx + 1] = newRight;

      onWidthsChange && onWidthsChange(next);
    },
    [dragIdx, minPaneWidthPct, onWidthsChange]
  );

  const handleMouseUp = useCallback(() => setDragIdx(null), []);

  useEffect(() => {
    if (dragIdx !== null) {
      document.addEventListener("mousemove", handleMouseMove);
      document.addEventListener("mouseup", handleMouseUp);
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    } else {
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }
    return () => {
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
  }, [dragIdx, handleMouseMove, handleMouseUp]);

  return (
    <div
      ref={containerRef}
      className={`multi-resizable-container ${dragIdx !== null ? "dragging" : ""}`}
    >
      {React.Children.map(children, (child, idx) => {
        const w = normWidths[idx] ?? 100 / count;
        return (
          <React.Fragment key={idx}>
            <div className="multi-resizable-pane" style={{ width: `${w}%` }}>
              {child}
            </div>
            {idx < count - 1 ? (
              <div className="multi-resize-handle" onMouseDown={(e) => handleMouseDown(idx, e)}>
                <div className="multi-resize-handle-line" />
              </div>
            ) : null}
          </React.Fragment>
        );
      })}
    </div>
  );
};
