{{/* SPDX-License-Identifier: AGPL-3.0-only */}}
{{/* One exact-origin list for session probes, CORS, and login return URLs. */}}
{{- define "platform-shared.authOrigins" -}}
{{- concat (list .Values.publicOrigin) .Values.authSession.allowedOrigins | uniq | toJson -}}
{{- end -}}
{{- define "platform-shared.authHostnames" -}}
{{- concat (list (urlParse .Values.adminOrigin).hostname) .Values.authIngress.additionalHostnames | uniq | toJson -}}
{{- end -}}
