"""Run ONLY in a fresh factory-startup Blender background process."""
import json
from pathlib import Path
import sys
import tempfile
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from verify_public import validate


def run():
    if not bpy.app.background or bpy.data.filepath:
        raise RuntimeError('Use a new --background --factory-startup process; never an open project')
    validate()
    report = {'status': 'RUNNING', 'blender': bpy.app.version_string, 'checks': []}
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        report['checks'].append(name)
        print('PASS:', name, flush=True)

    import petrify_painter as p
    registered = False
    try:
        with bpy.data.libraries.load(str(ROOT / 'petrify_painter/petrify_presets.blend'), link=False) as (src, dst):
            inventory = {key: list(getattr(src, key)) for key in dir(src)
                         if not key.startswith('_') and isinstance(getattr(src, key), list)}
        report['library_inventory'] = inventory
        check('library contains only the default node group', inventory.get('node_groups') == ['PP Preset Default'])
        for key in ('images', 'materials', 'objects', 'texts'):
            check('library has no ' + key, inventory.get(key) == [])
        # Load and inspect a separate default node-group data block, without any auto-execution.
        old_libraries = {x.as_pointer() for x in bpy.data.libraries}
        with bpy.data.libraries.load(str(ROOT / 'petrify_painter/petrify_presets.blend'), link=False) as (src, dst):
            dst.node_groups = ['PP Preset Default']
        # Blender can retain a source-library record after append (asset reuse metadata).
        # That is not a dependency on a second, external .blend file.
        introduced = [x for x in bpy.data.libraries if x.as_pointer() not in old_libraries]
        source_file = (ROOT / 'petrify_painter/petrify_presets.blend').resolve()
        check('no external libraries introduced', all(Path(bpy.path.abspath(x.filepath)).resolve() == source_file
                                                      for x in introduced))
        check('appended preset is local data', dst.node_groups[0].library is None)
        check('appended preset has no image textures', not any(node.type == 'TEX_IMAGE' for node in dst.node_groups[0].nodes))
        with tempfile.TemporaryDirectory(prefix='asfr-smoke-') as presets:
            p.custom_preset_directory = lambda: Path(presets)
            p.register()
            registered = True
            check('public edition', p.EDITION == 'PUBLIC')
            ob = bpy.data.objects.get('Cube')
            check('factory cube present', ob is not None and ob.type == 'MESH')
            p.prepare_object(bpy.context, ob, 16)
            p.ensure_volume(ob)
            s = bpy.context.scene.petrify_settings
            s.target = ob
            def apply(key):
                s.stone_preset = key
                check('apply ' + key, bpy.ops.petrify.apply_preset() == {'FINISHED'})
            for key in ('DEFAULT', 'DOLL', 'FROST', 'GOLD'):
                apply(key)
            s.gold_roughness = .42
            apply('DEFAULT')
            apply('GOLD')
            check('per-preset settings restored', abs(s.gold_roughness - .42) < 1e-5)
            def path():
                stroke = p.SphereStroke(p.VolumeCache(bpy.context, [ob]), 5, .8, 1)
                stroke.sample((0, 0, 0))
                return p.commit_stroke(bpy.context, stroke, True)
            first, second = path(), path()
            a, b = list(s.strokes)[-2:]
            a.enabled = False
            check('path disabled', first['asfr_visible'] == 0)
            a.enabled = True
            a.solo = True
            check('solo masks other paths', first['asfr_visible'] == 1 and second['asfr_visible'] == 0)
            a.solo = False
            a.selected = True
            b.selected = False
            old_start = a.start_frame
            s.path_shift = 3
            check('shift operation', bpy.ops.petrify.shift_paths() == {'FINISHED'})
            check('path time shifted', a.start_frame == old_start + 3)
            other = p.read_attribute(ob, second['weight_attr']).tobytes()
            s.erase_scope = 'SELECTED'
            stroke = p.SphereStroke(p.VolumeCache(bpy.context, [ob]), 5, .8, 1)
            stroke.sample((0, 0, 0))
            p.commit_stroke(bpy.context, stroke, False, True)
            check('erase selected path only', not any(p.read_attribute(ob, first['weight_attr'])) and
                  other == p.read_attribute(ob, second['weight_attr']).tobytes())
            check('delete path', bpy.ops.petrify.remove_stroke(index=0) == {'FINISHED'})
            check('other path remains', len(s.strokes) == 1)
            p.unregister()
            registered = False
            check('settings unregistered', not hasattr(bpy.types.Scene, 'petrify_settings'))
        report['status'] = 'PASS'
    except Exception:
        report['status'] = 'FAIL'
        report['error'] = traceback.format_exc()
        raise
    finally:
        if registered:
            p.unregister()
        output = ROOT / 'local-checks'
        output.mkdir(exist_ok=True)
        (output / 'blender_smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('ASFR PUBLIC BLENDER SMOKE: PASS', flush=True)


if __name__ == '__main__':
    run()
