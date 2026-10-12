"""Regression checks for the no-publish boundary and release token scope."""
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    # BaseLoader preserves GitHub's `on` key instead of YAML 1.1's boolean.
    return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)


def test_tag_workflows_and_called_builds_pin_every_external_action():
    paths = list((ROOT / '.github/workflows').glob('*.yml'))
    tag_paths = [p for p in paths if 'tags' in workflow(p.name).get('on', {}).get('push', {})]
    assert tag_paths
    visited = set()

    def check(path):
        if path in visited:
            return
        visited.add(path)
        data = workflow(path.name)
        for line in path.read_text().splitlines():
            if 'uses:' in line and '@' in line:
                assert re.search(r'uses: [\w./-]+@[a-f0-9]{40}\s+#\s+\S+', line), line
        for job in data['jobs'].values():
            if job.get('uses', '').startswith('./.github/workflows/'):
                check(ROOT / job['uses'])

    for path in tag_paths:
        check(path)


def test_manual_runs_cannot_reach_any_publisher_or_write_token():
    release = workflow('release.yml')
    assert 'workflow_dispatch' in release['on']
    assert release['permissions'] == {'contents': 'read'}
    jobs = release['jobs']
    publishers = {'package', 'image', 'github-release'}
    for name in publishers:
        # Both predicates are required: dispatching an existing tag is still safe.
        assert jobs[name]['if'] == "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    assert jobs['package']['environment'] == jobs['image']['environment'] == 'pypi'
    assert jobs['package']['permissions'] == {'contents': 'read', 'id-token': 'write'}
    assert jobs['image']['permissions'] == {'contents': 'read', 'packages': 'write'}
    assert jobs['github-release']['permissions'] == {'contents': 'write'}
    assert set(jobs['image']['needs']) == {'build', 'package'}
    assert set(jobs['github-release']['needs']) == {'package', 'image'}
    for name, job in jobs.items():
        if name not in publishers:
            assert 'permissions' not in job
        for step in job.get('steps', []):
            if step.get('with', {}).get('push') == 'true' or 'pypi-publish@' in step.get('uses', '') or 'gh release create' in step.get('run', ''):
                assert name in publishers


def test_ci_and_release_share_a_build_only_dry_run():
    release = workflow('release.yml')
    ci = workflow('ci.yml')
    assert release['jobs']['build']['uses'] == ci['jobs']['release-dry-run']['uses'] == './.github/workflows/release-build.yml'
    build = workflow('release-build.yml')
    assert build['permissions'] == {'contents': 'read'}
    assert set(build['on']) == {'workflow_call'}
    steps = build['jobs']['build']['steps']
    assert any(s.get('run') == 'uv build' for s in steps)
    assert any('check_dashboard_package.py' in s.get('run', '') for s in steps)
    image = next(s for s in steps if 'docker/build-push-action@' in s.get('uses', ''))
    assert image['with']['push'] == 'false'
    assert image['with']['load'] == 'true'
    assert any('docker run' in s.get('run', '') for s in steps)
    for step in steps:
        assert 'login-action@' not in step.get('uses', '')
        assert 'pypi-publish@' not in step.get('uses', '')
        assert 'gh release create' not in step.get('run', '')
        assert 'secrets.' not in str(step)
