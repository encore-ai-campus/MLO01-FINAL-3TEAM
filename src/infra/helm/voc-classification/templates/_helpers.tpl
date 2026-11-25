{{- define "voc.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* A pinned digest overrides the workstation tag. */}}
{{- define "voc.image" -}}
{{- if .digest -}}
{{ printf "%s@%s" .repository .digest }}
{{- else -}}
{{ printf "%s:%s" .repository .tag }}
{{- end -}}
{{- end -}}

{{- define "voc.modelName" -}}
{{- printf "%s-%s" (.root.Release.Name | trunc 30 | trimSuffix "-") .version -}}
{{- end -}}

{{- define "voc.workerName" -}}
{{- printf "%s-worker" .Release.Name -}}
{{- end -}}

{{- define "voc.security" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: [ALL]
{{- end -}}

{{- define "voc.podSecurity" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "voc.databaseEnv" -}}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.worker.databaseSecret.name | quote }}
      key: {{ .Values.worker.databaseSecret.key | quote }}
{{- end -}}
