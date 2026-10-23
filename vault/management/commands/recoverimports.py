"""Recover import jobs abandoned by a crashed worker or restarted server.

``recoverimports`` lists recoverable jobs; ``--run`` reconciles each against
its ledger and replays only uncommitted positions from a verified source. It
also drains already-accepted payloads for expired jobs without a replayable
upload and for cancelled jobs without resuming that terminal state. Live leases
are never taken over, and a changed source is refused rather than replayed
under old positions.
"""

from django.core.management.base import BaseCommand

from vault.import_app.import_engine import (
    discover_interrupted_jobs,
    lines_factory_for_job,
    recover_job,
)
from vault.models import ImportJob

ACTIVE = ('pending', 'processing', 'paused', 'interrupted', 'failed',
          'completed_with_errors')


class Command(BaseCommand):
    help = 'List (default) or recover (--run) abandoned durable import jobs.'

    def add_arguments(self, parser):
        parser.add_argument('--run', action='store_true',
                            help='Recover every listed job instead of listing.')
        parser.add_argument('--job', help='Only this job id (UUID).')

    def handle(self, *args, **options):
        jobs = ImportJob.objects.filter(status__in=ACTIVE).order_by('pk')
        if options['job']:
            jobs = jobs.filter(job_id=options['job'])
        candidates = {
            job.pk: job for job in jobs
            if job.can_resume()
            and not (
                job.status == 'paused' and job._has_live_lease()
            )
        }

        # Include stale jobs without a replayable source so their counters
        # can still be reconciled and accepted stage rows finalized. Missing
        # upload bytes remain an explicit unresolved gap; cancelled jobs are
        # cleaned up without resuming their terminal state.
        abandoned = discover_interrupted_jobs()
        if options['job']:
            abandoned = abandoned.filter(job_id=options['job'])
        for job in abandoned:
            if not job._has_live_lease():
                candidates.setdefault(job.pk, job)
        candidates = sorted(candidates.values(), key=lambda job: job.pk)

        if not candidates:
            self.stdout.write('No recoverable import jobs.')
            return
        for job in candidates:
            line = (
                f'{job.job_id} status={job.status} '
                f'committed={job.total_committed} skipped={job.total_skipped} '
                f'failed={job.total_failed} source={job.source_name}'
            )
            if not options['run']:
                self.stdout.write(line)
                continue
            source_factory = (
                None if job.status == 'cancelled'
                else lines_factory_for_job(job)
            )
            log = recover_job(job, lines_iterable_factory=source_factory)
            job.refresh_from_db()
            self.stdout.write(
                f'{line} -> recovery {log.outcome}; job now {job.status} '
                f'(committed={job.total_committed} skipped={job.total_skipped} '
                f'failed={job.total_failed})'
            )
