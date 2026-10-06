"""Portable, non-metallic glossy resin preset and per-material base colors."""
import bpy

KEY = 'DOLL'
ORIGINAL = 'Original Color'
REVISION = '3.3.0'
CONTROLS = {
    'resin_keep_color': 'Keep Original Color',
    'resin_color': 'Resin Color',
    'resin_roughness': 'Roughness',
    'resin_specular': 'Specular',
    'resin_coat': 'Coat Weight',
    'resin_coat_roughness': 'Coat Roughness',
}


def _socket(tree, name, kind, default=None, low=0, high=1, direction='INPUT'):
    s = tree.interface.new_socket(name=name, in_out=direction, socket_type=kind)
    if default is not None:s.default_value = default
    if kind == 'NodeSocketFloat':s.min_value=low;s.max_value=high
    return s


def _node(tree, kind, name, location=(0,0)):
    n=tree.nodes.new(kind);n.name=name;n.label=name;n.location=location
    return n


def template():
    for tree in bpy.data.node_groups:
        if tree.get('petrify_preset_template') == KEY and tree.get('petrify_preset_revision') == REVISION:
            return tree
    tree=bpy.data.node_groups.new('PP Preset Glossy Doll','ShaderNodeTree')
    _socket(tree,'Shader','NodeSocketShader',direction='OUTPUT')
    _socket(tree,'Alpha','NodeSocketFloat',1)
    _socket(tree,ORIGINAL,'NodeSocketColor',(0.8,0.65,0.58,1))
    _socket(tree,'Keep Original Color','NodeSocketFloat',1)
    _socket(tree,'Resin Color','NodeSocketColor',(0.8,0.65,0.58,1))
    _socket(tree,'Roughness','NodeSocketFloat',0.2,0.02,1)
    _socket(tree,'Specular','NodeSocketFloat',0.5)
    _socket(tree,'Coat Weight','NodeSocketFloat',0.65)
    _socket(tree,'Coat Roughness','NodeSocketFloat',0.12,0.02,1)
    inp=_node(tree,'NodeGroupInput','Doll Controls',(-650,80))
    color=_node(tree,'ShaderNodeMixRGB','Resin Palette',(-390,180))
    color.blend_type='MIX'
    tree.links.new(inp.outputs['Keep Original Color'],color.inputs[0])
    tree.links.new(inp.outputs['Resin Color'],color.inputs[1])
    tree.links.new(inp.outputs[ORIGINAL],color.inputs[2])
    surface=_node(tree,'ShaderNodeBsdfPrincipled','Glossy Resin',(-120,200))
    surface.inputs['Metallic'].default_value=0
    surface.inputs['IOR'].default_value=1.46
    surface.inputs['Coat IOR'].default_value=1.46
    surface.inputs['Subsurface Weight'].default_value=0
    surface.inputs['Transmission Weight'].default_value=0
    tree.links.new(color.outputs[0],surface.inputs['Base Color'])
    for src,dst in [('Roughness','Roughness'),('Specular','Specular IOR Level'),('Coat Weight','Coat Weight'),('Coat Roughness','Coat Roughness')]:
        tree.links.new(inp.outputs[src],surface.inputs[dst])
    transparent=_node(tree,'ShaderNodeBsdfTransparent','Preserve Transparency',(190,-160))
    mix=_node(tree,'ShaderNodeMixShader','Model Alpha',(410,150))
    tree.links.new(inp.outputs['Alpha'],mix.inputs[0])
    tree.links.new(transparent.outputs[0],mix.inputs[1])
    tree.links.new(surface.outputs[0],mix.inputs[2])
    out=_node(tree,'NodeGroupOutput','Resin Output',(630,150))
    tree.links.new(mix.outputs[0],out.inputs['Shader'])
    tree['petrify_preset_template']=KEY;tree['petrify_preset']=KEY
    tree['petrify_preset_revision']=REVISION
    tree['petrify_preset_label']='人偶硬质化 · 高光树脂'
    tree.use_fake_user=True
    return tree


def expose_original(tree):
    if not any(s.item_type=='SOCKET' and s.in_out=='INPUT' and s.name==ORIGINAL for s in tree.interface.items_tree):
        _socket(tree,ORIGINAL,'NodeSocketColor',(0.8,0.65,0.58,1))


def _copy_input(tree, source, target):
    if source.is_linked:tree.links.new(source.links[0].from_socket,target)
    else:target.default_value=source.default_value


def bind_original(material, rock):
    """Reuse source color links; never sample a lit shader or pick a normal map.

    Native Principled and MMD base texture/diffuse inputs are supported. Complex
    custom shaders fall back to material display color and report that limitation.
    No source nodes, textures, UVs or shader-output links are modified.
    """
    target=rock.inputs.get(ORIGINAL)
    if target is None:return
    tree=material.node_tree
    for link in tuple(target.links):tree.links.remove(link)
    target.default_value=material.diffuse_color
    material['petrify_resin_color_fallback']=False
    mmd=tree.nodes.get('mmd_shader')
    if mmd and all(mmd.inputs.get(k) for k in ('Diffuse Color','Base Tex','Base Tex Fac')):
        color=tree.nodes.get('Petrify Resin MMD Color')
        if color is None:color=_node(tree,'ShaderNodeMixRGB','Petrify Resin MMD Color',(380,-360))
        color.blend_type='MULTIPLY'
        for src,dst in [('Base Tex Fac',0),('Diffuse Color',1),('Base Tex',2)]:
            for link in tuple(color.inputs[dst].links):tree.links.remove(link)
            _copy_input(tree,mmd.inputs[src],color.inputs[dst])
        tree.links.new(color.outputs[0],target)
        return
    # Walk only the original surface branch, not our replacement shaders.
    roots=[]
    for n in tree.nodes:
        if n.type=='MIX_SHADER' and n.name.startswith('Petrify Mix'):
            roots.extend(l.from_node for l in n.inputs[1].links)
    if not roots:
        for n in tree.nodes:
            if n.type=='OUTPUT_MATERIAL':roots.extend(l.from_node for l in n.inputs['Surface'].links)
    seen=set();principals=[];candidates=[]
    while roots:
        node=roots.pop(0)
        if node.as_pointer() in seen or node.name.startswith('Petrify'):continue
        seen.add(node.as_pointer())
        if node.type=='BSDF_PRINCIPLED':principals.append(node)
        if node.type=='GROUP':
            color=next((s for s in node.inputs if s.type=='RGBA' and s.name.casefold() in {'base color','base colour','diffuse color','color','colour'}),None)
            if color:candidates.append(color)
        for socket in node.inputs:
            if socket.type=='SHADER':roots.extend(l.from_node for l in socket.links)
    if len(principals)==1:
        _copy_input(tree,principals[0].inputs['Base Color'],target)
    elif not principals and len(candidates)==1:
        _copy_input(tree,candidates[0],target)
    else:material['petrify_resin_color_fallback']=True


def control(tree, name):
    return tree.nodes['Preset Material'].inputs[CONTROLS[name]]


def update(settings, tree):
    # Reuse the existing continuous shader input: no new groups or textures.
    for prop in CONTROLS:
        if prop != 'resin_keep_color':
            control(tree,prop).default_value=getattr(settings,prop)
    mode=settings.resin_color_mode
    ratio=max(0.0,min(100.0,settings.resin_blend)) / 100.0
    factor=1.0 if mode=='ORIGINAL' else 0.0 if mode=='UNIFORM' else 1.0-ratio
    control(tree,'resin_keep_color').default_value=factor
    tree['petrify_resin_color_mode']=mode
    tree['petrify_resin_blend']=settings.resin_blend


def sync(settings, tree):
    for prop in CONTROLS:
        if prop != 'resin_keep_color':
            setattr(settings,prop,control(tree,prop).default_value)
    factor=max(0.0,min(1.0,control(tree,'resin_keep_color').default_value))
    mode=tree.get('petrify_resin_color_mode')
    if (mode not in {'ORIGINAL','UNIFORM','MIX'}
            or (mode=='ORIGINAL' and factor!=1) or (mode=='UNIFORM' and factor!=0)):
        mode='ORIGINAL' if factor==1 else 'UNIFORM' if factor==0 else 'MIX'
    settings.resin_keep_color=(mode=='ORIGINAL')
    settings.resin_color_mode=mode
    settings.resin_blend=(1-factor)*100 if mode=='MIX' else tree.get('petrify_resin_blend',50.0)
