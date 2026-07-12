from pathlib import Path
from sim_io.sim.run import _load_primary_psf_data

def test_noise_psf_loader_preserves_independent_frequency_axis(tmp_path):
 raw=tmp_path/'deck.raw'; raw.mkdir(); deck=tmp_path/'deck.scs'; deck.write_text('// deck')
 (raw/'onoise.noise').write_text('''HEADER\n"analysis type" "noise"\nTRACE\n"freq" "double"\n"VOUT" "double"\nVALUE\n"freq" 10\n"VOUT" 1e-9\n"freq" 100\n"VOUT" 2e-9\nEND\n''')
 data=_load_primary_psf_data(tmp_path,deck)
 assert data['noise_freq']==[10.0,100.0]
 assert data['noise:VOUT']==[1e-9,2e-9]

def test_noise_psf_loader_resolves_real_group_alias(tmp_path):
 raw=tmp_path/'deck.raw'; raw.mkdir(); deck=tmp_path/'deck.scs'; deck.write_text('// deck')
 (raw/'onoise.noise').write_text('''HEADER\n"analysis type" "noise"\nTRACE\n"freq" "Hz"\n" 1" GROUP 1\n"VOUT" "V/sqrt(Hz)"\nVALUE\n"freq" 10\n" 1" 1e-9\n"freq" 100\n" 1" 2e-9\nEND\n''')
 data=_load_primary_psf_data(tmp_path,deck)
 assert data['noise_freq']==[10.0,100.0]
 assert data['noise:VOUT']==[1e-9,2e-9]

def test_oppoint_psf_loader_maps_device_fields(tmp_path):
 raw=tmp_path/'deck.raw'; raw.mkdir(); deck=tmp_path/'deck.scs'; deck.write_text('// deck')
 (raw/'op.dcOp').write_text('''HEADER\n"analysis type" "dcOp"\nVALUE\n"M1:gm" 1e-3\n"M1:id" 1e-4\n"M1:gds" 1e-5\n"M1:vds" 1.2\n"M1:vdsat" 0.2\n"M1:region" 2\nEND\n''')
 data=_load_primary_psf_data(tmp_path,deck)
 assert data['op:M1']=={'gm':1e-3,'id':1e-4,'gds':1e-5,'vds':1.2,'vdsat':0.2,'region':2.0}
def test_oppoint_psf_loader_accepts_typed_hierarchical_real_rows(tmp_path):
 raw=tmp_path/'deck.raw'; raw.mkdir(); deck=tmp_path/'deck.scs'; deck.write_text('// deck')
 (raw/'op.dcOp').write_text('''HEADER\n"analysis type" "dcOp"\nVALUE\n"DUT.M1:gm" "S" 1e-3 PROP(\n"DUT/M1:id" "A" 1e-4\n"DUT.M1:gds" "S" 1e-5 PROP(foo)\nEND\n''')
 data=_load_primary_psf_data(tmp_path,deck)
 assert data['op:M1']=={'gm':1e-3,'id':1e-4,'gds':1e-5}


def test_oppoint_psf_loader_accepts_spectre_info_struct_rows(tmp_path):
 raw=tmp_path/'deck.raw'; raw.mkdir(); deck=tmp_path/'deck.scs'; deck.write_text('// deck')
 (raw/'opInfo.info').write_text('''HEADER
"analysis type" "info"
"AnalysisType" "op"
TYPE
"bsim3v3" STRUCT(
"ids" FLOAT DOUBLE PROP(
"units" "A"
)
"vds" FLOAT DOUBLE
"vdsat" FLOAT DOUBLE
"gm" FLOAT DOUBLE
"gds" FLOAT DOUBLE
)
VALUE
"DUT.M7" "bsim3v3" (
3.861e-3
0.709
0.123
0.109
8.04e-3
) PROP(
"model" "n33e2r"
)
END
''')
 data=_load_primary_psf_data(tmp_path,deck)
 assert data['op:M7']=={'id':3.861e-3,'vds':0.709,'vdsat':0.123,'gm':0.109,'gds':8.04e-3}
