"""Non-webhook inbound sources.

Most providers push inbound mail to ``POST /webhooks/inbound/{provider}``.
Alibaba Enterprise Mail has no such hook, so this package holds the polling
counterpart — see :mod:`src.inbound.alibaba_imap_poller`. Everything here
feeds the exact same
:meth:`~src.services.conversation_service.ConversationService.process_inbound`
pipeline the webhooks do.
"""
