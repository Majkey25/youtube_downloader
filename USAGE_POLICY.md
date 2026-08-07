# Hosted Service Usage Policy

Last updated: 7 August 2026

This policy applies to the hosted Media Downloader instance. The source code is
licensed separately under the repository's [MIT License](LICENSE).

## Permitted use

The hosted service is provided only for personal, lawful, non-commercial use.
You may submit media only when at least one of these conditions is true:

- you own the media and the necessary rights;
- the rights holder has given you permission to download it;
- the source clearly authorizes the download; or
- applicable law otherwise permits your specific use.

You must follow all applicable laws, regulations, licences, and the source
platform's terms. A technically accessible URL is not proof that downloading or
redistributing its media is lawful.

## Prohibited use

Do not use the hosted service to:

- pirate media or infringe copyright, neighbouring rights, privacy, publicity,
  contract, or other rights;
- circumvent DRM, encryption, paywalls, access controls, subscriptions,
  geographic controls, account restrictions, or technical protection measures;
- access login-only, private, stolen, leaked, or otherwise unauthorized media;
- submit cookies, passwords, tokens, account credentials, private URLs, or other
  authentication material;
- perform abusive automation, bulk scraping, denial-of-service activity, rate
  limit evasion, security testing without authorization, or other conduct that
  harms the service or a source platform;
- redistribute, publish, sell, sublicense, or otherwise exploit downloaded
  media without all required rights and permissions; or
- use the service for any unlawful or commercial purpose.

Attempts to bypass service limits or disabled features are also prohibited.

## Your responsibility

You are solely responsible for:

- every URL and request you submit;
- confirming that you have the required rights or permission before download;
- complying with the source platform's rules and applicable law;
- securing, storing, sharing, deleting, and otherwise using the resulting file;
  and
- any claim, penalty, account action, or loss arising from your use.

If you are unsure whether a use is permitted, do not proceed until the rights
holder or a qualified legal professional confirms it.

## Temporary files and privacy

A generated file is intended to be removed after one successful `GET` response
finishes. An abandoned file smaller than 64 MiB is targeted for cleanup after
10 minutes; a file at least 64 MiB is targeted after 30 minutes. Cleanup also
runs through a scheduled task intended to execute every minute. Failed job
artifacts are removed on a best-effort basis.

These are operational targets, not guarantees of immediate, permanent, or
secure erasure. Cleanup may be delayed by an outage, scheduler failure,
filesystem error, backup, logging, or hosting-provider behavior. Do not submit
sensitive, confidential, personal, private, or credential-bearing URLs or
media. Download and remove your output promptly.

## Availability and source restrictions

The service is provided **as is** and **as available**, without a guarantee of
uptime, compatibility, quality, completeness, fitness for a particular purpose,
or successful extraction. Source websites may change or block data-centre
traffic. Media may also be unavailable because of DRM, login, account,
subscription, geographic, legal, or platform restrictions. The service does not
promise to bypass any of them.

Access may be limited or refused to protect the service, the host, source
platforms, rights holders, or other users.

## Limitation of liability

To the maximum extent permitted by applicable law, the repository owner and
hosted-service maintainer are not liable for indirect, incidental, special,
consequential, or punitive loss, or for lost data, revenue, access, opportunity,
or claims arising from use of or inability to use the hosted service. Any
exclusion applies only where and to the extent the law allows it.

This policy is not legal advice. It cannot determine whether a particular use
is lawful, override mandatory law or platform terms, or guarantee protection
from legal claims, account enforcement, or other consequences.
