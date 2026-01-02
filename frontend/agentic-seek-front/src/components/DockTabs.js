import React from "react";
import "./DockTabs.css";

export const DockTabs = ({
  sideLabel,
  tabs,
  activeId,
  onSelect,
  onClose,
  onMove,
  hiddenTabs,
  onOpenHidden,
  onAddPane,
  onRemovePane,
  canRemovePane = false,
}) => {
  return (
    <div className="docktabs">
      <div className="docktabs-bar">
        <div className="docktabs-side">
          {sideLabel}
          {onAddPane ? (
            <button
              type="button"
              className="docktabs-pane-btn"
              onClick={onAddPane}
              title="Add pane"
            >
              +Pane
            </button>
          ) : null}
          {canRemovePane && onRemovePane ? (
            <button
              type="button"
              className="docktabs-pane-btn"
              onClick={onRemovePane}
              title="Remove pane"
            >
              −
            </button>
          ) : null}
        </div>
        <div className="docktabs-tabs">
          {tabs.map((t) => (
            <button
              key={t.id}
              className={`docktabs-tab ${t.id === activeId ? "active" : ""}`}
              onClick={() => onSelect(t.id)}
              title={t.title}
              type="button"
            >
              <span className="docktabs-tabtitle">{t.title}</span>
              <span className="docktabs-actions">
                <button
                  type="button"
                  className="docktabs-icon"
                  onClick={(e) => {
                    e.stopPropagation();
                    onMove(t.id);
                  }}
                  title="Move to next pane"
                >
                  ⇄
                </button>
                <button
                  type="button"
                  className="docktabs-icon"
                  onClick={(e) => {
                    e.stopPropagation();
                    onClose(t.id);
                  }}
                  title="Close tab"
                >
                  ×
                </button>
              </span>
            </button>
          ))}
        </div>
        <div className="docktabs-plus">
          <select
            className="docktabs-select"
            value=""
            onChange={(e) => {
              const v = e.target.value;
              if (!v) return;
              onOpenHidden(v);
              e.target.value = "";
            }}
          >
            <option value="">+ Add tab…</option>
            {hiddenTabs.map((t) => (
              <option key={t.id} value={t.id}>
                {t.title}
              </option>
            ))}
          </select>
        </div>
      </div>
    </div>
  );
};
