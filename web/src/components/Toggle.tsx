/** A labelled on/off switch for settings. */
export function Toggle({ label, hint, checked, onChange, disabled }: {
  label: string; hint?: string; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean;
}) {
  return (
    <label className={`toggle-row${disabled ? " is-disabled" : ""}`}>
      <span className="toggle-text">
        <span className="setting-label">{label}</span>
        {hint && <span className="dim toggle-hint">{hint}</span>}
      </span>
      <button type="button" role="switch" aria-checked={checked} disabled={disabled}
        className={`switch${checked ? " on" : ""}`} onClick={() => onChange(!checked)}>
        <span />
      </button>
    </label>
  );
}
