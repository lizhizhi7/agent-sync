"""Exercise the resource CLI against committed synthetic resources, without user homes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class Exports(unittest.TestCase):
    def test_locked_export_ignores_worktree_and_checks_supporting_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'source';source.mkdir();(source/'.agent-sync-data').touch()
            def git(*args):
                return subprocess.run(['git','-C',str(source),*args],check=True,capture_output=True).stdout
            git('init','-q');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
            git('remote','add','origin','https://example.invalid/resources.git')
            skill=source/'skills/demo';skill.mkdir(parents=True)
            (skill/'SKILL.md').write_text('original');(skill/'reference.md').write_text('supporting')
            (skill/'.upstream').write_text('ref=original')
            git('add','.');git('commit','-qm','fixture')
            entry=root/'installed bin'/'agent-sync';entry.parent.mkdir();entry.symlink_to(ROOT/'bin/agent-sync')
            request=root/'request.json';locked=root/'resources.lock';dest=root/'candidate'
            request.write_text(json.dumps({'version':1,'resources':[{'id':'common/demo','path':'skills/demo'}]}))
            def run(*args):
                return subprocess.run(['/bin/bash',str(entry),'resource',*map(str,args),'--source-dir',str(source)],capture_output=True,env={**os.environ,'HOME':str(root/'home'),'AGENT_SYNC_DIR':str(source),'AGENT_SYNC_AGENTS':'claude','AGENT_SYNC_BACKEND':'local'})
            self.assertEqual(run('lock',request,'--output',locked).returncode,0)
            (skill/'reference.md').write_text('uncommitted')
            self.assertEqual(run('export',locked,'--destination',dest).returncode,0)
            self.assertEqual((dest/'common/demo/reference.md').read_text(),'supporting')
            self.assertEqual((dest/'common/demo/.upstream').read_text(),'ref=original')
            self.assertNotEqual(run('export',locked,'--destination',dest).returncode,0)
            data=json.loads(locked.read_text());data['resources'][0]['files']['reference.md']['sha256']='0'*64
            locked.write_text(json.dumps(data));other=root/'rejected'
            self.assertNotEqual(run('export',locked,'--destination',other).returncode,0)
            self.assertFalse(other.exists())
            (skill/'escape').symlink_to('/etc/passwd');git('add','.');git('commit','-qm','link')
            self.assertNotEqual(run('lock',request,'--output',root/'link.lock').returncode,0)
            request.write_text(json.dumps({'version':1,'resources':[{'id':'../escape','path':'skills/demo'}]}))
            self.assertNotEqual(run('lock',request,'--output',root/'escape.lock').returncode,0)

    def test_skill_alias_must_resolve_inside_its_source_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'upstream';source.mkdir();data=root/'data';data.mkdir()
            (data/'.agent-sync-data').touch()
            def git(*args):return subprocess.run(['git','-C',str(source),*args],check=True,capture_output=True)
            git('init','-q');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
            (source/'package/skill').mkdir(parents=True)
            (source/'package/skill/SKILL.md').write_text('Declared skill')
            (source/'alias').symlink_to('package/skill',target_is_directory=True)
            outside=root/'outside';outside.mkdir();(outside/'SKILL.md').write_text('Must not export')
            (source/'escape').symlink_to(outside,target_is_directory=True)
            git('add','.');git('commit','-qm','alias fixture')
            def add(name,path):
                return subprocess.run(['/bin/bash',str(ROOT/'bin/agent-sync'),'skill','add',name,str(source),path],capture_output=True,
                    env={**os.environ,'HOME':str(root/'home'),'AGENT_SYNC_DIR':str(data),'AGENT_SYNC_AGENTS':'claude','AGENT_SYNC_BACKEND':'local'})
            result=add('selected','alias')
            self.assertEqual(result.returncode,0,result.stderr.decode())
            self.assertEqual((data/'skills/selected/SKILL.md').read_text(),'Declared skill')
            self.assertNotEqual(add('escaped','escape').returncode,0)
            self.assertFalse((data/'skills/escaped').exists())


if __name__=='__main__':unittest.main()
