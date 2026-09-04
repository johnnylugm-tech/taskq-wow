"""[FR-01] taskq-api package — Phase-3 source root.

The package layout mirrors `02-architecture/SAD.md` §2.7 (api/service/
repository/models). Each layer has its own sub-package; the SAB.json
declaration in `.methodology/SAB.json` enumerates the leaf modules this
package must export. Phase-3 GREEN agents populate leaf modules per FR
contracts; Phase-2 only ships the preflight config-key stub at
`taskq_api._p2_preflight_config_keys`.

Citations:
- SPEC.md §2 — module layout: api/service/repository/models layers.
- SAD.md §2.7 — `taskq_api.api.{app,dependencies,tasks,runs,health,metrics}`,
  `taskq_api.service.{tasks,auth,rate_limit,runner}`,
  `taskq_api.repository.{session,tasks,api_keys,rate_buckets,results,tags}`,
  `taskq_api.models.{task,api_key,rate_bucket,result,tag}`.
"""