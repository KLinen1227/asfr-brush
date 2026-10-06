"""Golden conductor preset: shared procedural shader, per-target controls.

No textures are baked, no geometry is added, and no source material is edited.
The ordinary Alpha and Normal inputs remain compatible with Petrify's wrappers.
"""
import bpy

KEY = 'GOLD'
REVISION = '3.6.0'
LABEL = '黄金化 · 金属雕像'
COLOR = (.83, .49, .105, 1)
CONTROLS = {
    'gold_color': 'Gold Color',
    'gold_metallic': 'Metallic',
    'gold_roughness': 'Roughness',
    'gold_texture': 'Texture Strength',
    'gold_scale': 'Texture Scale',
}


def template():
    for tree in bpy.data.node_groups:
        if (tree.get('petrify_preset_template') == KEY
                and tree.get('petrify_preset_revision') == REVISION):
            return tree
    tree = bpy.data.node_groups.new('PP Preset Golden Statue', 'ShaderNodeTree')
    tree.interface.new_socket(name='Shader', in_out='OUTPUT', socket_type='NodeSocketShader')
    for name, kind, value, lo, hi in (
        ('Alpha', 'NodeSocketFloat', 1, 0, 1),
        ('Gold Color', 'NodeSocketColor', COLOR, 0, 1),
        ('Metallic', 'NodeSocketFloat', 1, 0, 1),
        ('Roughness', 'NodeSocketFloat', .18, .02, 1),
        ('Texture Strength', 'NodeSocketFloat', .06, 0, 1),
        ('Texture Scale', 'NodeSocketFloat', 80, 1, 500),
    ):
        socket = tree.interface.new_socket(name=name, in_out='INPUT', socket_type=kind)
        socket.default_value = value
        if kind == 'NodeSocketFloat':
            socket.min_value = lo
            socket.max_value = hi

    def node(kind, name, xy):
        n = tree.nodes.new(kind)
        n.name = n.label = name
        n.location = xy
        return n

    inp = node('NodeGroupInput', 'Gold Controls', (-800, 200))
    coord = node('ShaderNodeTexCoord', 'Stable Surface Coordinates', (-800, -180))
    grain = node('ShaderNodeTexNoise', 'Fine Gold Texture', (-560, -120))
    grain.noise_dimensions = '3D'
    grain.inputs['Detail'].default_value = 2
    grain.inputs['Roughness'].default_value = .55
    bump = node('ShaderNodeBump', 'Gold Microtexture', (-300, -50))
    bump.inputs['Distance'].default_value = .003
    surface = node('ShaderNodeBsdfPrincipled', 'Golden Metal', (0, 200))
    surface.inputs['Coat Weight'].default_value = 0
    surface.inputs['Subsurface Weight'].default_value = 0
    surface.inputs['Transmission Weight'].default_value = 0
    surface.inputs['IOR'].default_value = 1.5
    transparent = node('ShaderNodeBsdfTransparent', 'Preserve Transparency', (280, -150))
    mix = node('ShaderNodeMixShader', 'Model Alpha', (480, 180))
    out = node('NodeGroupOutput', 'Gold Output', (710, 180))
    link = tree.links.new
    link(coord.outputs['Generated'], grain.inputs['Vector'])
    link(inp.outputs['Texture Scale'], grain.inputs['Scale'])
    link(grain.outputs['Fac'], bump.inputs['Height'])
    link(inp.outputs['Texture Strength'], bump.inputs['Strength'])
    link(bump.outputs['Normal'], surface.inputs['Normal'])
    for src, dst in (('Gold Color', 'Base Color'), ('Metallic', 'Metallic'), ('Roughness', 'Roughness')):
        link(inp.outputs[src], surface.inputs[dst])
    link(inp.outputs['Alpha'], mix.inputs[0])
    link(transparent.outputs[0], mix.inputs[1])
    link(surface.outputs[0], mix.inputs[2])
    link(mix.outputs[0], out.inputs['Shader'])
    tree['petrify_preset_template'] = KEY
    tree['petrify_preset'] = KEY
    tree['petrify_preset_revision'] = REVISION
    tree['petrify_preset_label'] = LABEL
    tree.use_fake_user = True
    return tree


def control(tree, prop):
    return tree.nodes['Preset Material'].inputs[CONTROLS[prop]]


def update(settings, tree):
    for prop in CONTROLS:
        control(tree, prop).default_value = getattr(settings, prop)


def sync(settings, tree):
    for prop in CONTROLS:
        setattr(settings, prop, control(tree, prop).default_value)


def draw(layout, settings):
    for prop in CONTROLS:
        layout.prop(settings, prop)
    layout.label(text='粗糙度低：镜面金 / 高：磨砂金')
    layout.label(text='金属度 1 为金属；纹理 0 为光滑')
    layout.label(text='高光依赖灯光；轻量预览不显示金属光泽')
