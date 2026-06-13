Includes = {
	"buttonstate.fxh"
	"sprite_animation.fxh"
}

PixelShader =
{
	Samplers =
	{
		MapTexture =
		{
			Index = 0
			MagFilter = "Linear"
			MinFilter = "Linear"
			MipFilter = "None"
			AddressU = "Clamp"
			AddressV = "Clamp"
			MipMapLodBias = -0.8
		}
		MaskTexture =
		{
			Index = 1
			MagFilter = "Linear"
			MinFilter = "Linear"
			MipFilter = "None"
			AddressU = "Clamp"
			AddressV = "Clamp"
		}
		AnimatedTexture =
		{
			Index = 2
			MagFilter = "Linear"
			MinFilter = "Linear"
			MipFilter = "None"
			AddressU = "Wrap"
			AddressV = "Wrap"
		}
		MaskTexture2 =
		{
			Index = 3
			MagFilter = "Linear"
			MinFilter = "Linear"
			MipFilter = "None"
			AddressU = "Clamp"
			AddressV = "Clamp"
		}
		AnimatedTexture2 =
		{
			Index = 4
			MagFilter = "Linear"
			MinFilter = "Linear"
			MipFilter = "None"
			AddressU = "Wrap"
			AddressV = "Wrap"
		}
		#This masking texture is the ACTUAL masking texture. The others are for animation
		MaskingTexture =
		{
			Index = 5
			MagFilter = "Point"
			MinFilter = "Point"
			MipFilter = "None"
			AddressU = "Clamp"
			AddressV = "Clamp"
		}




	}
}


VertexStruct VS_OUTPUT
{
	float4  vPosition : PDX_POSITION;
	float2  vTexCoord : TEXCOORD0;
@ifdef ANIMATED
	float4  vAnimatedTexCoord : TEXCOORD1;
@endif
@ifdef MASKING
	float2  vMaskingTexCoord : TEXCOORD2;
@endif
};


VertexShader =
{
	MainCode VertexShader
	[[
		VS_OUTPUT main(const VS_INPUT v )
		{
		    VS_OUTPUT Out;
		    Out.vPosition  = mul( WorldViewProjectionMatrix, float4( v.vPosition.xyz, 1 ) );
		
			float value = (Offset.x * 10.f) + 1.f;
			float2 trueOffset = Offset;
			float currentPos = floor(value/10000.f);
			float totalPos = floor(value/10.f) - (currentPos * 1000.f);
			trueOffset.x = floor(value - (currentPos * 10000.f) - (totalPos * 10.f));
			if(trueOffset.x == 0){
				trueOffset.x = 10.f;
			}
			trueOffset.x = (trueOffset.x - 1.f)/10.f;
		    Out.vTexCoord = v.vTexCoord;
			Out.vTexCoord += trueOffset;
			
		#ifdef ANIMATED
			Out.vAnimatedTexCoord = GetAnimatedTexcoord(v.vTexCoord);	
		#endif

		#ifdef MASKING
			//A bit hacky, but we want the masking texture coordinates to be in the range [0,1]. We turn all 0's to 0 and all nonzero to 1.
			Out.vMaskingTexCoord = saturate(v.vTexCoord * 1000); 
		#endif
		
		    return Out;
		}
	]]
}

PixelShader =
{
	MainCode PixelShaderUp
	[[
		float4 main( VS_OUTPUT v ) : PDX_COLOR
		{
		    float4 OutColor = tex2D( MapTexture, v.vTexCoord );
			float timeMult = 0.5f;
			float timeDiv = timeMult/1.f;
			float value = (Offset.x * 10.f) + 1.f;
			float currentPos = floor(value/10000.f);
			float totalPos = floor(value/10.f) - (currentPos * 1000.f);
			float pos = value - (currentPos * 10000.f) - (totalPos * 10.f);
			if(pos < 1){
				return OutColor;
			}
			
			float vTime = (Time - AnimationTime);
			float sinTime = cos(vTime * timeMult);
			sinTime = (sinTime - 1.f)/-2.f;
			float timePos = floor(totalPos * sinTime);
			float colourCheck = 0.f;
			if(OutColor.r > 0.2 && OutColor.r < 0.5 && OutColor.g > 0.6 && OutColor.g < 0.9 && OutColor.b > 0.6 && OutColor.b < 0.9){
				colourCheck = 1.f;
			}
			if(OutColor.a > 0 && OutColor.a < 1){
				OutColor.a = 1.f;
			}
			if(timePos <= currentPos && (vTime < 6.5) && colourCheck == 0) {
				return float4(0,0,0,0);
			}
			return OutColor;
		}
	]]

	MainCode PixelShaderDown
	[[
		float4 main( VS_OUTPUT v ) : PDX_COLOR
		{
		    float4 OutColor = tex2D( MapTexture, v.vTexCoord );
					
		#ifdef ANIMATED
			OutColor = Animate(OutColor, v.vTexCoord, v.vAnimatedTexCoord, MaskTexture, AnimatedTexture, MaskTexture2, AnimatedTexture2);
		#endif

		#ifdef MASKING
			float4 MaskColor = tex2D( MaskingTexture, v.vTexCoord );
			OutColor.a *= MaskColor.a;
		#endif
			
			OutColor *= Color;

			float vTime = 0.9 - saturate( (Time - AnimationTime) * 16 );
			vTime *= vTime;
			vTime = 0.9*0.9 - vTime;
		    float4 MixColor = float4( 0.15, 0.15, 0.15, 0 ) * vTime;
		    OutColor.rgb -= ( 0.5 + OutColor.rgb ) * MixColor.rgb;

			return OutColor;
		}
	]]

	MainCode PixelShaderDisable
	[[
		float4 main( VS_OUTPUT v ) : PDX_COLOR
		{
		    float4 OutColor = tex2D( MapTexture, v.vTexCoord );
			float timeMult = 0.5f;
			float timeDiv = timeMult/1.f;
			float value = (Offset.x * 10.f) + 1.f;
			float currentPos = floor(value/10000.f);
			float totalPos = floor(value/10.f) - (currentPos * 1000.f);
			float pos = value - (currentPos * 10000.f) - (totalPos * 10.f);
			
			if(pos < 1){
				return OutColor;
			}
			
			float vTime = (Time - AnimationTime);
			float sinTime = cos(vTime * timeMult);
			sinTime = (sinTime - 1.f)/-2.f;
			float timePos = floor(totalPos * sinTime);
			float colourCheck = 0.f;
			if(OutColor.r > 0.2 && OutColor.r < 0.5 && OutColor.g > 0.6 && OutColor.g < 0.9 && OutColor.b > 0.6 && OutColor.b < 0.9){
				colourCheck = 1.f;
			}
			if(OutColor.a > 0 && OutColor.a < 1){
				OutColor.a = 1.f;
			}
			if(timePos <= currentPos && (vTime < 6.5) && colourCheck == 0) {
				return float4(0,0,0,0);
			}
			return OutColor;
		}
	]]

	MainCode PixelShaderOver
	[[
		float4 main( VS_OUTPUT v ) : PDX_COLOR
		{
		    float4 OutColor = tex2D( MapTexture, v.vTexCoord );
				
		#ifdef ANIMATED
			OutColor = Animate(OutColor, v.vTexCoord, v.vAnimatedTexCoord, MaskTexture, AnimatedTexture, MaskTexture2, AnimatedTexture2);
		#endif

		#ifdef MASKING
			float4 MaskColor = tex2D( MaskingTexture, v.vTexCoord );
			OutColor.a *= MaskColor.a;
		#endif

			OutColor *= Color;
			
			float vTime = 0.9 - saturate( (Time - AnimationTime) * 4 );
			vTime *= vTime;
			vTime = 0.9*0.9 - vTime;
		    float4 MixColor = float4( 0.15, 0.15, 0.15, 0 ) * vTime;
		    OutColor.rgb += ( 0.5 + OutColor.rgb ) * MixColor.rgb;
			
			return OutColor;
		}
	]]
}


BlendState BlendState
{
	BlendEnable = yes
	SourceBlend = "src_alpha"
	DestBlend = "inv_src_alpha"
}


Effect Up
{
	VertexShader = "VertexShader"
	PixelShader = "PixelShaderUp"
}

Effect Down
{
	VertexShader = "VertexShader"
	PixelShader = "PixelShaderDown"
}

Effect Disable
{
	VertexShader = "VertexShader"
	PixelShader = "PixelShaderDisable"
}

Effect Over
{
	VertexShader = "VertexShader"
	PixelShader = "PixelShaderOver"
}

