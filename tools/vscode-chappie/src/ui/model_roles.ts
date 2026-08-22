import { ControlPlaneModelV1, DevelopmentModelRoleV1 } from "../api/contracts";

export interface SelectedModelRoleV1 { role: DevelopmentModelRoleV1; model: ControlPlaneModelV1 | undefined; }

/** The backend inventory is authoritative; this UI only selects a stable role. */
export function selectRole(models: readonly ControlPlaneModelV1[], role: DevelopmentModelRoleV1): SelectedModelRoleV1 {
  return { role, model: models.find((item) => item.role === role) };
}

export function roleLabel(selection: SelectedModelRoleV1): string {
  return selection.model ? `${selection.role} — ${selection.model.concrete_model}` : `${selection.role} — unavailable`;
}

export function roleUnavailableMessage(selection: SelectedModelRoleV1): string | null {
  return !selection.model || !selection.model.ready ? `Selected model role ${selection.role} is unavailable. Select an available role; Chappie will not fall back automatically.` : null;
}
