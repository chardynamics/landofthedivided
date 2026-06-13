Includes = {
}

PixelShader =
{
	Samplers =
	{
		TextureOne =
		{
			Index = 0
			MagFilter = "Point"
			MinFilter = "Point"
			MipFilter = "None"
			AddressU = "Wrap"
			AddressV = "Wrap"
		}
		TextureTwo =
		{
			Index = 1
			MagFilter = "Point"
			MinFilter = "Point"
			MipFilter = "None"
			AddressU = "Wrap"
			AddressV = "Wrap"
		}
	}
}


VertexStruct VS_INPUT
{
    float4 vPosition  : POSITION;
    float2 vTexCoord  : TEXCOORD0;
};

VertexStruct VS_OUTPUT
{
    float4  vPosition : PDX_POSITION;
    float2  vTexCoord0 : TEXCOORD0;
};


ConstantBuffer( 0, 0 )
{
	float4x4 WorldViewProjectionMatrix; 
	float4 vFirstColor;
	float4 vSecondColor;
	float CurrentState;
};


VertexShader =
{
	MainCode VertexShader
	[[
		
		VS_OUTPUT main(const VS_INPUT v )
		{
			VS_OUTPUT Out;
		   	Out.vPosition  = mul( WorldViewProjectionMatrix, v.vPosition );
			Out.vTexCoord0  = v.vTexCoord;
			Out.vTexCoord0.y = -Out.vTexCoord0.y;
		
			return Out;
		}
		
	]]
}

PixelShader =
{
	MainCode PixelColor
	[[
		
		float4 main( VS_OUTPUT v ) : PDX_COLOR
		{
			if( v.vTexCoord0.x <= CurrentState )
				return vFirstColor;
			else
				return vSecondColor;
		}
		
	]]

	MainCode PixelTexture
	[[
		
		float4 main( VS_OUTPUT v ) : PDX_COLOR
		{
			float value = CurrentState * 10000.f;
			float colourValue = floor(value/1000.f);
			float progress = (value - (colourValue * 1000.f))/100.f;
			float4 Text1 = float4(0,0,0,0);
			float colourPicker = colourValue/9.f;
			float4 Text2 = tex2D(TextureTwo, float2(colourPicker - 0.001f, v.vTexCoord0.y));
			
			if( (v.vTexCoord0.y > -progress) ) {
				float distance = (1.f + v.vTexCoord0.y) + progress;
				float4 Text1 = tex2D( TextureOne, float2(v.vTexCoord0.x,distance) );
				if( Text1.r == 0.f && Text1.a > 0) {
					return tex2D(TextureTwo, float2(colourPicker - 0.001f, v.vTexCoord0.y));
				}
				else {
					return tex2D( TextureOne, float2(v.vTexCoord0.x,distance) );
				}
			}
			else {
				return float4(0, 0, 0, 0);
			}
		}
		
	]]
}


BlendState BlendState
{
	BlendEnable = yes
	SourceBlend = "SRC_ALPHA"
	DestBlend = "INV_SRC_ALPHA"
}


Effect Color
{
	VertexShader = "VertexShader"
	PixelShader = "PixelColor"
}

Effect Texture
{
	VertexShader = "VertexShader"
	PixelShader = "PixelTexture"
}

