# SPDX-License-Identifier: AGPL-3.0-only
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit, unquote
from deployment_credentials import database_url, initial_identity, read_env, tenant_secrets
from deployment_config import ConfigError
import test_deployment_render


class DeploymentCredentialTests(test_deployment_render.DeploymentRenderTests):
    def test_secret_adapter_matches_rendered_model_and_database_references(self):
        resolved = self.resolved()
        identity = initial_identity('example')
        tls = {role: {'tls.crt':'synthetic-cert','tls.key':'synthetic-key','ca.crt':'synthetic-ca'}
               for role in ['server','orchestrator','management','anonymous','model-catalog','customer-worker']}
        env = {'EXAMPLE_API_KEY':'synthetic-model-key', 'EMBED_MODAL_KEY':'synthetic-modal-key', 'EMBED_MODAL_SECRET':'synthetic-modal-secret', 'STORAGE_AWS_ACCESS_KEY_ID':'synthetic-storage-key',
               'STORAGE_AWS_SECRET_ACCESS_KEY':'synthetic-storage-secret',
               'BACKUP_AWS_ACCESS_KEY_ID':'synthetic-backup-key','BACKUP_AWS_SECRET_ACCESS_KEY':'synthetic-backup-secret'}
        docs = {d['metadata']['name']:d for d in tenant_secrets(resolved,identity,tls,env)}
        gateway = docs['gateway-files']['stringData']
        self.assertIn('models-example-secrets.json',gateway)
        callers = json.loads(gateway['callers.json'])['clients']
        platform = next(c for c in callers if c['subject'] == 'platform-server')
        self.assertIn('agent', platform['allowed_attribution'])
        memory = next(c for c in callers if c['subject'] == 'memorylayer')
        self.assertNotIn('agent', memory['allowed_attribution'])
        self.assertEqual(json.loads(gateway['models-example-secrets.json']),{'review-api_key_env':'synthetic-model-key'})
        self.assertIn('aether-sparkroute-creds-example',docs)
        self.assertNotIn('synthetic-model-key',gateway['config.json'])
        for name in ['memorylayer-db','connectors-db','storage-db','gateway-db']:
            self.assertEqual(docs[name]['type'],'kubernetes.io/basic-auth')
        for name,field in [('memorylayer-env','MEMORYLAYER_POSTGRESQL_URL'),('connectors-env','DC_POSTGRESQL_URL'),
                           ('gateway-env','SPARKROUTE_POSTGRES_URL'),('storage-env','BLOBGW_DATABASE_URL')]:
            self.assertEqual(urlsplit(docs[name]['stringData'][field]).hostname,'example-postgres-db-rw')
        for name, field in [('memorylayer-env', 'MEMORYLAYER_POSTGRESQL_URL'), ('connectors-env', 'DC_POSTGRESQL_URL')]:
            url = urlsplit(docs[name]['stringData'][field])
            self.assertEqual(url.scheme, 'postgresql+asyncpg')
            self.assertEqual(url.query, 'ssl=require')
        source = docs['usage-producer']['stringData']
        self.assertEqual(source['BILLING_PRODUCER_TOKEN'],identity['usageProducerToken'])

    def test_workload_identity_does_not_need_static_storage_keys(self):
        resolved = self.resolved()
        resolved['bindings']['objectStorage'].update(credentialMode='aws-sts',roleARN='arn:aws:iam::123456789012:role/example')
        resolved['bindings']['backup']['serviceAccountAnnotations']={'eks.amazonaws.com/role-arn':'arn:aws:iam::123456789012:role/backup'}
        docs={d['metadata']['name']:d for d in tenant_secrets(resolved,initial_identity('example'),{},
                                                           {'EXAMPLE_API_KEY':'synthetic-model-key', 'EMBED_MODAL_KEY':'synthetic-modal-key', 'EMBED_MODAL_SECRET':'synthetic-modal-secret'})}
        self.assertNotIn('AWS_SECRET_ACCESS_KEY',docs['storage-env']['stringData'])
        self.assertEqual(docs['storage-files']['stringData']['credentials.json'],'{}')
        self.assertNotIn('backup-credentials',docs)

    def test_literal_env_parser_does_not_execute_and_url_quotes_credentials(self):
        path=self.root/'private.env'
        path.write_text("API_KEY='literal $(touch /tmp/do-not-execute) `echo unsafe`'\nSECOND=abc # note\n")
        env=read_env(path)
        self.assertEqual(env['API_KEY'],'literal $(touch /tmp/do-not-execute) `echo unsafe`')
        self.assertEqual(env['SECOND'],'abc')
        path.write_text('API_KEY=one\nAPI_KEY=two\n')
        with self.assertRaises(ConfigError):read_env(path)
        url=urlsplit(database_url('example','p@ss/?#','db'))
        self.assertEqual(unquote(url.password),'p@ss/?#')
        self.assertEqual(url.query,'sslmode=require')
